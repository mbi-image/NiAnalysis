from past.builtins import basestring
from builtins import object
from itertools import chain
import sys
import os.path as op
import types
from logging import getLogger
from arcana.exception import (
    ArcanaMissingDataException, ArcanaNameError, ArcanaUsageError,
    ArcanaMissingInputError, ArcanaNoConverterError, ArcanaDesignError,
    ArcanaCantPickleStudyError)
from arcana.pipeline import Pipeline
from arcana.dataset import (
    BaseData, BaseField, DatasetSpec)
from nipype.pipeline import engine as pe
from arcana.parameter import Parameter, Switch
from arcana.interfaces.iterators import (
    InputSessions, InputSubjects)
from arcana.node import Node
from arcana.interfaces.repository import (RepositorySource,
                                          RepositorySink)

logger = getLogger('arcana')


class Study(object):
    """
    Abstract base study class from which all study derive.

    Parameters
    ----------
    name : str
        The name of the study.
    repository : Repository
        An Repository object that provides access to a DaRIS, XNAT or local file
        system
    processor : Processor
        A Processor to process the pipelines required to generate the
        requested derived datasets.
    inputs : Dict[str, DatasetMatch | DatasetSpec | FieldMatch | FieldSpec] | List[DatasetMatch | DatasetSpec | FieldMatch | FieldSpec]
        Either a list or a dictionary containing DatasetMatch,
        FieldMatch, DatasetSpec, or FieldSpec objects, which specify the
        names of input datasets to the study, i.e. those that won't
        be generated by this study (although can be derived by the parent
        MultiStudy)
    parameters : List[Parameter] | Dict[str, (int|float|str)]
        Parameters that are passed to pipelines when they are constructed
        either as a dictionary of key-value pairs or as a list of
        'Parameter' objects. The name and dtype must match ParameterSpecs in
        the _parameter_spec class attribute (see 'add_parameter_specs').
    switches : List[Switch] | Dict[str, str]
        Switches that are used to specify which analysis branches to
        follow, i.e. which method to select out of several comparable
        methods
    subject_ids : List[(int|str)]
        List of subject IDs to restrict the analysis to
    visit_ids : List[(int|str)]
        List of visit IDs to restrict the analysis to
    enforce_inputs : bool
        Whether to check the inputs to see if any acquired datasets
        are missing
    reprocess : bool
        Whether to reprocess dataset|fields that have been created with
        different parameters and/or pipeline-versions. If False then
        and exception will be thrown if the repository already contains
        matching datasets|fields created with different parameters.
    fill_tree : bool
        Whether to fill the tree of the destination repository with the
        provided subject and/or visit IDs. Only really useful if the
        destination repository doesn't contain any of the the input
        datasets/fields (which are stored in external repositories) and
        so the sessions will need to be created in the destination
        repository.


    Class Attrs
    -----------
    add_data_specs : List[DatasetSpec|FieldSpec]
        Adds specs to the '_data_specs' class attribute,
        which is a dictionary that maps the names of datasets that are
        used and generated by the study to (Dataset|Field)Spec objects.
    add_parameter_specs : List[ParameterSpec]
        Adds specs to the '_parameter_specs' class attribute,
        which is a dictionary that maps the names of parameters that are
        provided to pipelines in the study
    add_switch_specs : List[SwitchSpec]
        Adds switch specs to the '_switch_specs' class attribute,
        which is a dictionary that maps the names of switches that are
        used to switch between comparable pipeline methods.
    """

    _data_specs = {}
    _parameter_specs = {}
    _switch_specs = {}

    implicit_cls_attrs = ['_data_specs', '_parameter_specs',
                          '_switch_specs']

    def __init__(self, name, repository, processor, inputs, parameters=None,
                 switches=None, subject_ids=None, visit_ids=None,
                 enforce_inputs=True, reprocess=False, fill_tree=False):
        try:
            # This works for PY3 as the metaclass inserts it itself if
            # it isn't provided
            metaclass = type(self).__dict__['__metaclass__']
            if not issubclass(metaclass, StudyMetaClass):
                raise KeyError
        except KeyError:
            raise ArcanaUsageError(
                "Need to have StudyMetaClass (or a sub-class) as "
                "the metaclass of all classes derived from Study")
        self._name = name
        self._repository = repository
        self._processor = processor.bind(self)
        self._inputs = {}
        self._subject_ids = subject_ids
        self._visit_ids = visit_ids
        self._tree = self.repository.cached_tree(
            subject_ids=subject_ids,
            visit_ids=visit_ids,
            fill=fill_tree)
        if not self.subject_ids:
            raise ArcanaUsageError(
                "No subject IDs provided and destination repository "
                "is empty")
        if not self.visit_ids:
            raise ArcanaUsageError(
                "No visit IDs provided and destination repository "
                "is empty")
        self._reprocess = reprocess
        # Convert inputs to a dictionary if passed in as a list/tuple
        if not isinstance(inputs, dict):
            inputs = {i.name: i for i in inputs}
        # Add each "input dataset" checking to see whether the given
        # dataset_spec name is valid for the study types
        for inpt_name, inpt in list(inputs.items()):
            try:
                spec = self.data_spec(inpt_name)
            except ArcanaNameError:
                raise ArcanaNameError(
                    inpt.name,
                    "Input name '{}' isn't in data specs of {} ('{}')"
                    .format(
                        inpt.name, self.__class__.__name__,
                        "', '".join(self._data_specs)))
            else:
                if isinstance(spec, DatasetSpec):
                    if isinstance(inpt, BaseField):
                        raise ArcanaUsageError(
                            "Passed field ({}) as input to dataset spec"
                            " {}".format(inpt, spec))
                    try:
                        spec.format.converter_from(inpt.format)
                    except ArcanaNoConverterError as e:
                        raise ArcanaNoConverterError(
                            "{}, which is requried to convert:\n{} "
                            "to\n{}.".format(e, inpt, spec))
                elif not isinstance(inpt, BaseField):
                    raise ArcanaUsageError(
                        "Passed dataset ({}) as input to field spec {}"
                        .format(inpt, spec))
            self._inputs[inpt_name] = inpt.bind(self)
        # "Bind" data specs in the class to the current study object
        # this will allow them to prepend the study name to the name
        # of the dataset
        self._bound_specs = {}
        for spec in self.data_specs():
            if spec.name not in self.input_names:
                if not spec.derived:
                    # Emit a warning if an acquired dataset has not been
                    # provided for an "acquired dataset"
                    msg = (" acquired dataset '{}' was not given as"
                           " an input of {}.".format(spec.name, self))
                    if spec.optional:
                        logger.info('Optional' + msg)
                    else:
                        if enforce_inputs:
                            raise ArcanaMissingInputError(
                                'Non-optional' + msg + " Pipelines "
                                "depending on this dataset will not "
                                "run")
                else:
                    self._bound_specs[spec.name] = spec.bind(self)
        # Set switches
        if switches is None:
            switches = {}
        elif not isinstance(switches, dict):
            switches = {s.name: s for s in switches}
        self._switches = {}
        # Set switches
        for switch_name, switch in list(switches.items()):
            if not isinstance(switch, Switch):
                switch = Switch(switch_name, switch)
            try:
                switch_spec = self._switch_specs[switch_name]
            except KeyError:
                raise ArcanaNameError(
                    "Provided switch '{}' is not present in the "
                    "allowable switches for {} classes ('{}')"
                    .format(switch_name, type(self).__name__,
                            "', '".join(self.default_switches)))
            switch_spec.check_valid(switch, ' provided to {}'
                                    .format(self))
            self._switches[switch_name] = switch
        # Set parameters
        if parameters is None:
            parameters = {}
        elif not isinstance(parameters, dict):
            # Convert list of parameters into dictionary
            parameters = {o.name: o for o in parameters}
        self._parameters = {}
        for param_name, param in list(parameters.items()):
            if not isinstance(param, Parameter):
                param = Parameter(param_name, param)
            try:
                param_spec = self._parameter_specs[param_name]
            except KeyError:
                raise ArcanaNameError(
                    param_name,
                    "Provided parameter '{}' is not present in the "
                    "allowable parameters for {} classes ('{}')"
                    .format(param_name, type(self).__name__,
                            "', '".join(self.parameter_spec_names())))
            if param.value is not None and not isinstance(
                    param.value, param_spec.dtype):
                raise ArcanaUsageError(
                    "Incorrect datatype for '{}' parameter provided "
                    "to {}(name='{}') (as '{}'), ({}). Should be {}"
                    .format(param.name, type(self).__name__, name,
                            param_name, type(param.value),
                            param_spec.dtype))
            if (param_spec.choices is not None and
                    param.value not in param_spec.choices):
                raise ArcanaUsageError(
                    "Provided value for '{}' parameter in {}(name='{}') "
                    "(as '{}') , {}, is not a valid choice. Can be one "
                    "of {}"
                    .format(param.name, type(self).__name__, name,
                            param_name, param.value,
                            ','.join(param_spec.choices)))
            self._parameters[param_name] = param
        # For recording which parameters and switches are accessed
        # during pipeline generation so they can be attributed to the
        # pipeline after it is generated (and then saved in the
        # provenance
        self._pipeline_to_generate = None
        self._referenced_parameters = None
        self._referenced_switches = None

    def __repr__(self):
        """String representation of the study"""
        return "{}(name='{}')".format(self.__class__.__name__,
                                      self.name)

    def __reduce__(self):
        """
        Control how study classes are pickled to allow some generated
        classes (those that don't define additional methods) to be
        generated
        """
        cls = type(self)
        module = sys.modules[cls.__module__]
        try:
            # Check whether the study class is generated or not by
            # seeing if it exists in its module
            if cls is not getattr(module, cls.__name__):
                raise AttributeError
        except AttributeError:
            cls_dct = {}
            for name, attr in list(cls.__dict__.items()):
                if isinstance(attr, types.FunctionType):
                    try:
                        if not attr.auto_added:
                            raise ArcanaCantPickleStudyError()
                    except (AttributeError, ArcanaCantPickleStudyError):
                        raise ArcanaCantPickleStudyError(
                            "Cannot pickle auto-generated study class "
                            "as it contains non-auto-added method "
                            "{}:{}".format(name, attr))
                elif name not in self.implicit_cls_attrs:
                    cls_dct[name] = attr
            pkld = (pickle_reconstructor,
                    (cls.__metaclass__, cls.__name__, cls.__bases__,
                     cls_dct), self.__dict__)
        else:
            # Use standard pickling if not a generated class
            pkld = object.__reduce__(self)
        return pkld

    @property
    def tree(self):
        return self._tree

    @property
    def processor(self):
        return self._processor

    @property
    def inputs(self):
        return list(self._inputs.values())

    @property
    def input_names(self):
        return list(self._inputs.keys())

    def input(self, name):
        try:
            return self._inputs[name]
        except KeyError:
            raise ArcanaNameError(
                name,
                "{} doesn't have an input named '{}'"
                .format(self, name))

    @property
    def missing_inputs(self):
        return (n for n in self.acquired_data_spec_names()
                if n not in self._inputs)

    @property
    def subject_ids(self):
        if self._subject_ids is None:
            return [s.id for s in self.tree.subjects]
        return self._subject_ids

    @property
    def visit_ids(self):
        if self._visit_ids is None:
            return [v.id for v in self.tree.visits]
        return self._visit_ids

    @property
    def prefix(self):
        """The study name as a prefix for dataset names"""
        return self.name + '_'

    @property
    def name(self):
        """Accessor for the unique study name"""
        return self._name

    @property
    def reprocess(self):
        return self._reprocess

    @property
    def repository(self):
        "Accessor for the repository member (e.g. Daris, XNAT, MyTardis)"
        return self._repository

    def create_pipeline(self, *args, **kwargs):
        """
        Creates a Pipeline object, passing the study (self) as the first
        argument
        """
        return Pipeline(self, *args, **kwargs)

    def _get_parameter(self, name):
        try:
            parameter = self._parameters[name]
        except KeyError:
            try:
                parameter = self._parameter_specs[name]
            except KeyError:
                raise ArcanaNameError(
                    name,
                    "Invalid parameter, '{}', in {} (valid '{}')"
                    .format(
                        name, self._param_error_location,
                        "', '".join(self.parameter_spec_names())))
        return parameter

    def _get_switch(self, name):
        try:
            switch = self._switches[name]
        except KeyError:
            try:
                switch = self._switch_specs[name]
            except KeyError:
                raise ArcanaNameError(
                    name,
                    "Invalid switch, '{}', in {} (valid '{}')".format(
                        name, self._param_error_location,
                        "', '".join(self.switch_spec_names())))
        return switch

    def parameter(self, name):
        """
        Retrieves the value of the parameter and registers the parameter
        as being used by this pipeline for use in provenance capture

        Parameters
        ----------
        name : str
            The name of the parameter to retrieve
        """
        if self._referenced_parameters is not None:
            self._referenced_parameters.add(name)
        return self._get_parameter(name).value

    def switch(self, name):  # @UnusedVariable @IgnorePep8
        """
        Retrieves the value of the switch and registers the parameter
        as being used by this pipeline for use in provenance capture

        Parameters
        ----------
        name : str
            The name of the parameter to retrieve
        """
        if self._referenced_switches is not None:
            self._referenced_switches.add(name)
        return self._get_switch(name).value

    def branch(self, name, values):  # @UnusedVariable @IgnorePep8
        """
        Checks whether the given switch matches the value provided

        Parameters
        ----------
        name : str
            The name of the parameter to retrieve
        value : str | None
            The value of the switch to match if a non-boolean switch
        """
        if isinstance(values, basestring):
            values = [values]
        spec = self.switch_spec(name)
        if spec.is_boolean:
            raise ArcanaDesignError(
                "Boolean switch '{}' in {} should not be used in a "
                "'branch' call".format(
                    name, self._param_error_location))
        switch = self._get_switch(name)
        # Register parameter as being used by the pipeline
        unrecognised_values = set(values) - set(spec.choices)
        if unrecognised_values:
            raise ArcanaDesignError(
                "Provided value(s) ('{}') for switch '{}' in {} "
                "is not a valid option ('{}')".format(
                    "', '".join(unrecognised_values), name,
                    self._param_error_location,
                    "', '".join(spec.choices)))
        if self._referenced_switches is not None:
            self._referenced_switches.add(name)
        return switch.value in values

    def unhandled_branch(self, name):
        """
        Convenient method for raising exception if a pipeline doesn't
        handle a particular switch value

        Parameters
        ----------
        name : str
            Name of the switch
        value : str
            Value of the switch which hasn't been handled
        """
        raise ArcanaDesignError(
            "'{}' value of '{}' switch in {} is not handled"
            .format(self._get_switch(name), name,
                    self._param_error_location))

    @property
    def _param_error_location(self):
        return ("generation of '{}' pipeline of {}"
                .format(self._pipeline_to_generate, self))

    @property
    def parameters(self):
        for param_name in self._parameter_specs:
            yield self._get_parameter(param_name)

    @property
    def switches(self):
        for name in self._switch_specs:
            yield self._get_switch(name)

    def data(self, name, subject_id=None, visit_id=None):
        """
        Returns the Dataset or Field associated with the name,
        generating derived datasets as required. Multiple names in a
        list can be provided, in which case their workflows are
        joined into a single workflow.

        Parameters
        ----------
        name : str | List[str]
            The name of the DatasetSpec|FieldSpec to retried the
            datasets for
        subject_id : int | str | List[int|str] | None
            The subject ID or subject IDs to return. If None all are
            returned
        visit_id : int | str | List[int|str] | None
            The visit ID or visit IDs to return. If None all are
            returned

        Returns
        -------
        data : Dataset | Field | List[Dataset | Field] | List[List[Dataset | Field]]
            If a single name is provided then data is either a single
            Dataset or field if a single subject_id and visit_id are
            provided, otherwise a list of datasets or fields
            corresponding to the given name. If muliple names are
            provided then a list is returned containing the data for
            each provided name.
        """
        if isinstance(name, basestring):
            single_name = True
            names = [name]
        else:
            names = name
            single_name = False
        def is_single_id(id_):  # @IgnorePep8
            return isinstance(id_, (basestring, int))
        subject_ids = ([subject_id]
                       if is_single_id(subject_id) else subject_id)
        visit_ids = ([visit_id] if is_single_id(visit_id) else visit_id)
        # Work out which pipelines need to be run
        pipelines = []
        for name in names:
            try:
                pipelines.append(self.spec(name).pipeline)
            except AttributeError:
                pass  # Match objects don't have pipelines
        # Run all pipelines together
        if pipelines:
            self.processor.run(
                *pipelines, subject_ids=subject_ids,
                visit_ids=visit_ids)
        all_data = []
        for name in names:
            spec = self.spec(name)
            data = spec.collection
            if subject_ids is not None and spec.frequency in (
                    'per_session', 'per_subject'):
                data = [d for d in data if d.subject_id in subject_ids]
            if visit_ids is not None and spec.frequency in (
                    'per_session', 'per_visit'):
                data = [d for d in data if d.visit_id in visit_ids]
            if not data:
                raise ArcanaUsageError(
                    "No matching data found (subject_id={}, visit_id={})"
                    .format(subject_id, visit_id))
            if is_single_id(subject_id) and is_single_id(visit_id):
                assert len(data) == 1
                data = data[0]
            else:
                data = spec.CollectionClass(spec.name, data)
            if single_name:
                return data
            all_data.append(data)
        return all_data

    def save_workflow_graph_for(self, spec_name, fname, full=False,
                                style='flat', **kwargs):
        """
        Saves a graph of the workflow to generate the requested spec_name

        Parameters
        ----------
        spec_name : str
            Name of the spec to generate the graph for
        fname : str
            The filename for the saved graph
        style : str
            The style of the graph, can be one of can be one of
            'orig', 'flat', 'exec', 'hierarchical'
        """
        pipeline = self.spec(spec_name).pipeline
        if full:
            workflow = pe.Workflow(name='{}_gen'.format(spec_name),
                                   base_dir=self.processor.work_dir)
            self.processor._connect_to_repository(
                pipeline, workflow, **kwargs)
        else:
            workflow = pipeline._workflow
        fname = op.expanduser(fname)
        if not fname.endswith('.png'):
            fname += '.png'
        dotfilename = fname[:-4] + '.dot'
        workflow.write_graph(graph2use=style,
                             dotfilename=dotfilename)

    def spec(self, name):
        """
        Returns either the input corresponding to a dataset or field
        field spec or a spec or parameter that has either
        been passed to the study as an input or can be derived.

        Parameters
        ----------
        name : Str | BaseData | Parameter
            An parameter, dataset or field or name of one
        """
        if isinstance(name, (BaseData, Parameter)):
            name = name.name
        try:
            spec = self._inputs[name]
        except KeyError:
            try:
                spec = self._bound_specs[name]
            except KeyError:
                if name in self._data_specs:
                    raise ArcanaMissingDataException(
                        "Acquired (i.e. non-generated) dataset '{}' "
                        "was not supplied when the study '{}' was "
                        "initiated".format(name, self.name))
                else:
                    try:
                        spec = self._parameter_specs[name]
                    except KeyError:
                        try:
                            spec = self._switch_specs[name]
                        except KeyError:
                            raise ArcanaNameError(
                                name,
                                "'{}' is not a recognised spec name "
                                "for {} studies:\n{}."
                                .format(name, self.__class__.__name__,
                                        '\n'.join(sorted(
                                            self.spec_names()))))
        return spec

    @classmethod
    def data_spec(cls, name):
        """
        Return the dataset_spec, i.e. the template of the dataset expected to
        be supplied or generated corresponding to the dataset_spec name.

        Parameters
        ----------
        name : Str
            Name of the dataset_spec to return
        """
        if isinstance(name, BaseData):
            name = name.name
        try:
            return cls._data_specs[name]
        except KeyError:
            raise ArcanaNameError(
                name,
                "No dataset spec named '{}' in {} (available: "
                "'{}')".format(name, cls.__name__,
                               "', '".join(list(cls._data_specs.keys()))))

    @classmethod
    def parameter_spec(cls, name):
        try:
            return cls._parameter_specs[name]
        except KeyError:
            raise ArcanaNameError(
                name,
                "No parameter spec named '{}' in {} (available: "
                "'{}')".format(name, cls.__name__,
                               "', '".join(list(cls._parameter_specs.keys()))))

    @classmethod
    def switch_spec(cls, name):
        try:
            return cls._switch_specs[name]
        except KeyError:
            raise ArcanaNameError(
                name,
                "No switch spec named '{}' in {} (available: "
                "'{}')".format(name, cls.__name__,
                               "', '".join(list(cls._switch_specs.keys()))))

    @classmethod
    def data_specs(cls):
        """Lists all data_specs defined in the study class"""
        return iter(cls._data_specs.values())

    @classmethod
    def parameter_specs(cls):
        return iter(cls._parameter_specs.values())

    @classmethod
    def switch_specs(cls):
        return iter(cls._switch_specs.values())

    @classmethod
    def data_spec_names(cls):
        """Lists the names of all data_specs defined in the study"""
        return iter(cls._data_specs.keys())

    @classmethod
    def parameter_spec_names(cls):
        """Lists the names of all parameter_specs defined in the study"""
        return iter(cls._parameter_specs.keys())

    @classmethod
    def switch_spec_names(cls):
        """Lists the names of all switch_specs defined in the study"""
        return iter(cls._switch_specs.keys())

    @classmethod
    def spec_names(cls):
        return chain(cls.data_spec_names(),
                     cls.parameter_spec_names(),
                     cls.switch_spec_names())

    @classmethod
    def acquired_data_specs(cls):
        """
        Lists all data_specs defined in the study class that are
        provided as inputs to the study
        """
        return (c for c in cls.data_specs() if not c.derived)

    @classmethod
    def derived_data_specs(cls):
        """
        Lists all data_specs defined in the study class that are typically
        generated from other data_specs (but can be overridden by input
        datasets)
        """
        return (c for c in cls.data_specs() if c.derived)

    @classmethod
    def derived_data_spec_names(cls):
        """Lists the names of generated data_specs defined in the study"""
        return (c.name for c in cls.derived_data_specs())

    @classmethod
    def acquired_data_spec_names(cls):
        "Lists the names of acquired data_specs defined in the study"
        return (c.name for c in cls.acquired_data_specs())

    def cache_inputs(self):
        """
        Runs the Study's repository source node for each of the inputs
        of the study, thereby caching any data required from remote
        repositorys. Useful when launching many parallel jobs that will
        all try to concurrently access the remote repository, and probably
        lead to timeout errors.
        """
        workflow = pe.Workflow(name='cache_download',
                               base_dir=self.processor.work_dir)
        subjects = pe.Node(InputSubjects(), name='subjects')
        sessions = pe.Node(InputSessions(), name='sessions')
        subjects.iterables = ('subject_id', tuple(self.subject_ids))
        sessions.iterables = ('visit_id', tuple(self.visit_ids))
        source = self.source(self.inputs)
        workflow.connect(subjects, 'subject_id', sessions, 'subject_id')
        workflow.connect(sessions, 'subject_id', source, 'subject_id')
        workflow.connect(sessions, 'visit_id', source, 'visit_id')
        workflow.run()

    def source(self, inputs, name='source'):
        """
        Returns a NiPype node that gets the input data from the repository
        system. The input spec of the node's interface should inherit from
        RepositorySourceInputSpec

        Parameters
        ----------
        project_id : str
            The ID of the project to return the sessions for
        inputs : list(Dataset|Field)
            An iterable of arcana.Dataset or arcana.Field
            objects, which specify the datasets to extract from the
            repository system
        name : str
            Name of the NiPype node
        from_study: str
            Prefix used to distinguish datasets generated by a particular
            study. Used for derived datasets only
        """
        return Node(RepositorySource(
            self.spec(i).collection for i in inputs), name=name)

    def sink(self, outputs, frequency='per_session', name=None):
        """
        Returns a NiPype node that puts the output data back to the repository
        system. The input spec of the node's interface should inherit from
        RepositorySinkInputSpec

        Parameters
        ----------
        project_id : str
            The ID of the project to return the sessions for
        outputs : List(BaseFile|Field) | list(
            An iterable of arcana.Dataset arcana.Field objects,
            which specify the datasets to put into the repository system
        name : str
            Name of the NiPype node
        from_study: str
            Prefix used to distinguish datasets generated by a particular
            study. Used for derived datasets only

        """
        if name is None:
            name = '{}_sink'.format(frequency)
        return Node(RepositorySink(
            (self.spec(o).collection for o in outputs),
            frequency), name=name)


class StudyMetaClass(type):
    """
    Metaclass for all study classes that collates data specs from
    bases and checks pipeline method names.

    Combines specifications in add_(data|parameter|switch)_specs from
    the class to be created with its base classes, overriding matching
    specs in the order of the bases.
    """

    def __new__(metacls, name, bases, dct):  # @NoSelf @UnusedVariable
        if not any(issubclass(b, Study) for b in bases):
            raise ArcanaUsageError(
                "StudyMetaClass can only be used for classes that "
                "have Study as a base class")
        try:
            add_data_specs = dct['add_data_specs']
        except KeyError:
            add_data_specs = []
        try:
            add_parameter_specs = dct['add_parameter_specs']
        except KeyError:
            add_parameter_specs = []
        try:
            add_switch_specs = dct['add_switch_specs']
        except KeyError:
            add_switch_specs = []
        combined_attrs = set()
        combined_data_specs = {}
        combined_parameter_specs = {}
        combined_switch_specs = {}
        for base in reversed(bases):
            # Get the combined class dictionary including base dicts
            # excluding auto-added properties for data and parameter specs
            combined_attrs.update(
                a for a in dir(base) if (not issubclass(base, Study) or
                                         a not in base.spec_names()))
            try:
                combined_data_specs.update(
                    (d.name, d) for d in base.data_specs())
            except AttributeError:
                pass
            try:
                combined_parameter_specs.update(
                    (p.name, p) for p in base.parameter_specs())
            except AttributeError:
                pass
            try:
                combined_switch_specs.update(
                    (s.name, s) for s in base.switch_specs())
            except AttributeError:
                pass
        combined_attrs.update(list(dct.keys()))
        combined_data_specs.update((d.name, d) for d in add_data_specs)
        combined_parameter_specs.update(
            (p.name, p) for p in add_parameter_specs)
        combined_switch_specs.update(
            (s.name, s) for s in add_switch_specs)
        # Check that the pipeline names in data specs correspond to a
        # pipeline method in the class
        for spec in add_data_specs:
            pipe_name = spec.pipeline_name
            if pipe_name is not None and pipe_name not in combined_attrs:
                raise ArcanaUsageError(
                    "Pipeline to generate '{}', '{}', is not present"
                    " in '{}' class".format(
                        spec.name, spec.pipeline_name, name))
        # Check for name clashes between data and parameter specs
        spec_name_clashes = (set(combined_data_specs) &
                             set(combined_parameter_specs) &
                             set(combined_switch_specs))
        if spec_name_clashes:
            raise ArcanaUsageError(
                "'{}' name both data and parameter specs in '{}' class"
                .format("', '".join(spec_name_clashes), name))
        dct['_data_specs'] = combined_data_specs
        dct['_parameter_specs'] = combined_parameter_specs
        dct['_switch_specs'] = combined_switch_specs
        if '__metaclass__' not in dct:
            dct['__metaclass__'] = metacls
        return type(name, bases, dct)


def pickle_reconstructor(metacls, name, bases, cls_dict):
    obj = DummyObject()
    obj.__class__ = metacls(name, bases, cls_dict)
    return obj


class DummyObject(object):
    pass
