from itertools import chain
from nipype.interfaces.utility import IdentityInterface
from arcana.exception import (
    ArcanaMissingDataException, ArcanaNameError)
from arcana.pipeline import Pipeline
from arcana.exception import ArcanaUsageError
from .base import Study, StudyMetaClass


class MultiStudy(Study):
    """
    Abstract base class for all studies that combine multiple studies into a
    a combined study

    Parameters
    ----------
    name : str
        The name of the combined study.
    archive : Archive
        An Archive object that provides access to a DaRIS, XNAT or local file
        system
    runner : Runner
        The runner the processes the derived data when demanded
    inputs : Dict[str, Dataset|Field]
        A dict containing the a mapping between names of study data_specs
        and existing datasets (typically primary from the scanner but can
        also be replacements for generated data_specs)
    options : List[Option] | Dict[str, (int|float|str)]
        Options that are passed to pipelines when they are constructed
        either as a dictionary of key-value pairs or as a list of
        'Option' objects. The name and dtype must match OptionSpecs in
        the _option_spec class attribute (see 'add_option_specs').
    subject_ids : List[(int|str)]
        List of subject IDs to restrict the analysis to
    visit_ids : List[(int|str)]
        List of visit IDs to restrict the analysis to
    check_inputs : bool
        Whether to check the inputs to see if any acquired datasets
        are missing
    reprocess : bool
        Whether to reprocess dataset|fields that have been created with
        different parameters and/or pipeline-versions. If False then
        and exception will be thrown if the archive already contains
        matching datasets|fields created with different parameters.

    Class Attributes
    ----------------
    add_sub_study_specs : list[SubStudySpec]
        Subclasses of MultiStudy typically have a 'add_sub_study_specs'
        class member, which defines the sub-studies that make up the
        combined study and the mapping of their dataset names. The key
        of the outer dictionary will be the name of the sub-study, and
        the value is a tuple consisting of the class of the sub-study
        and a map of dataset names from the combined study to the
        sub-study e.g.

            add_sub_study_specs = [
                SubStudySpec('t1_study', MRIStudy, {'t1': 'mr_scan'}),
                SubStudySpec('t2_study', MRIStudy, {'t2': 'mr_scan'})]

            add_data_specs = [
                DatasetSpec('t1', nifti_gz_format'),
                DatasetSpec('t2', nifti_gz_format')]
    add_data_specs : List[DatasetSpec|FieldSpec]
        Add's that data specs to the 'data_specs' class attribute,
        which is a dictionary that maps the names of datasets that are
        used and generated by the study to DatasetSpec objects.
    add_option_specs : List[OptionSpec]
        Default options for the class
    """

    _sub_study_specs = {}

    def __init__(self, name, archive, runner, inputs, options=None,
                 **kwargs):
        options = [] if options is None else options
        try:
            if not issubclass(type(self).__dict__['__metaclass__'],
                              MultiStudyMetaClass):
                raise KeyError
        except KeyError:
            raise ArcanaUsageError(
                "Need to set MultiStudyMetaClass (or sub-class) as "
                "the metaclass of all classes derived from "
                "MultiStudy")
        super(MultiStudy, self).__init__(name, archive, runner, inputs,
                                         options=options, **kwargs)
        self._sub_studies = {}
        for sub_study_spec in self.sub_study_specs():
            # Create copies of the input datasets to pass to the
            # __init__ method of the generated sub-studies
            sub_study_cls = sub_study_spec.study_class
            mapped_inputs = []
            for inpt in inputs:
                try:
                    mapped_inputs.append(
                        inpt.renamed(sub_study_spec.map(inpt.name)))
                except ArcanaNameError:
                    pass  # Ignore datasets not required for sub-study
            mapped_options = []
            for opt_name in sub_study_cls.option_spec_names():
                mapped_name = sub_study_spec.inverse_map(opt_name)
                option = self._get_option(mapped_name)
                mapped_options.append(option.renamed(opt_name))
            # Create sub-study
            sub_study = sub_study_spec.study_class(
                name + '_' + sub_study_spec.name,
                archive, runner, mapped_inputs,
                options=mapped_options,
                enforce_inputs=False)
#             # Set sub-study as attribute
#             setattr(self, sub_study_spec.name, sub_study)
            # Append to dictionary of sub_studies
            if sub_study_spec.name in self._sub_studies:
                raise ArcanaNameError(
                    sub_study_spec.name,
                    "Duplicate sub-study names '{}'"
                    .format(sub_study_spec.name))
            self._sub_studies[sub_study_spec.name] = sub_study

    @property
    def sub_studies(self):
        return self._sub_studies.itervalues()

    @property
    def sub_study_names(self):
        return self._sub_studies.iterkeys()

    def sub_study(self, name):
        try:
            return self._sub_studies[name]
        except KeyError:
            raise ArcanaNameError(
                name,
                "'{}' not found in sub-studes ('{}')"
                .format(name, "', '".join(self._sub_studies)))

    @classmethod
    def sub_study_spec(cls, name):
        try:
            return cls._sub_study_specs[name]
        except KeyError:
            raise ArcanaNameError(
                name,
                "'{}' not found in sub-studes ('{}')"
                .format(name, "', '".join(cls._sub_study_specs)))

    @classmethod
    def sub_study_specs(cls):
        return cls._sub_study_specs.itervalues()

    def __repr__(self):
        return "{}(name='{}')".format(
            self.__class__.__name__, self.name)

    @classmethod
    def translate(cls, sub_study_name, pipeline_name, add_inputs=None,
                  add_outputs=None, **kwargs):
        """
        A "decorator" (although not intended to be used with @) for
        translating pipeline getter methods from a sub-study of a
        MultiStudy. Returns a new method that calls the getter on
        the specified sub-study then translates the pipeline to the
        MultiStudy.

        Parameters
        ----------
        sub_study_name : str
            Name of the sub-study
        pipeline_name : str
            Unbound method used to create the pipeline in the sub-study
        add_inputs : list[str]
            List of additional inputs to add to the translated pipeline
            to be connected manually in combined-study getter (i.e. not
            using translate_getter decorator).
        add_outputs : list[str]
            List of additional outputs to add to the translated pipeline
            to be connected manually in combined-study getter (i.e. not
            using translate_getter decorator).
        """
        assert isinstance(sub_study_name, basestring)
        assert isinstance(pipeline_name, basestring)
        def translated_getter(self, name_prefix='',  # @IgnorePep8
                              add_inputs=add_inputs,
                              add_outputs=add_outputs):
            trans_pipeline = TranslatedPipeline(
                self, self.sub_study(sub_study_name),
                pipeline_name, name_prefix=name_prefix,
                add_inputs=add_inputs, add_outputs=add_outputs, **kwargs)
            trans_pipeline.assert_connected()
            return trans_pipeline
        return translated_getter


class SubStudySpec(object):
    """
    Specify a study to be included in a MultiStudy class

    Parameters
    ----------
    name : str
        Name for the sub-study
    study_class : type (sub-classed from Study)
        The class of the sub-study
    name_map : dict[str, str]
        A mapping of dataset/field/option names from the MultiStudy
        scope to the scopy of the sub-study (i.e. the _data_specs dict
        in the class of the sub-study). All data-specs that are not
        explicitly provided in this mapping are auto-translated using
        the sub-study prefix.
    """

    def __init__(self, name, study_class, name_map=None):
        self._name = name
        self._study_class = study_class
        # Fill dataset map with default values before overriding with
        # argument provided to constructor
        self._name_map = name_map if name_map is not None else {}
        self._inv_map = dict((v, k) for k, v in self._name_map.items())

    @property
    def name(self):
        return self._name

    def __repr__(self):
        return "{}(name='{}', cls={}, name_map={}".format(
            type(self).__name__, self.name, self.study_class,
            self._name_map)

    @property
    def study_class(self):
        return self._study_class

    @property
    def name_map(self):
        nmap = dict((self.apply_prefix(s.name), s.name)
                    for s in self.auto_data_specs)
        nmap.update(self._name_map)
        return nmap

    def map(self, name):
        try:
            return self._name_map[name]
        except KeyError:
            mapped = self.strip_prefix(name)
            if mapped not in chain(self.study_class.data_spec_names(),
                                   self.study_class.option_spec_names()):
                raise ArcanaNameError(
                    name,
                    "'{}' has a matching prefix '{}_' but '{}' doesn't"
                    " match any datasets, fields or options in the "
                    "study class {} ('{}')"
                    .format(name, self.name, mapped,
                            self.study_class.__name__,
                            "', '".join(
                                self.study_class.data_spec_names())))
            return mapped

    def inverse_map(self, name):
        try:
            return self._inv_map[name]
        except KeyError:
            if name not in chain(self.study_class.data_spec_names(),
                                 self.study_class.option_spec_names()):
                raise ArcanaNameError(
                    name,
                    "'{}' doesn't match any datasets, fields or options"
                    " in the study class {} ('{}')"
                    .format(name, self.study_class.__name__,
                            "', '".join(
                                self.study_class.data_spec_names())))
            return self.apply_prefix(name)

    def apply_prefix(self, name):
        return self.name + '_' + name

    def strip_prefix(self, name):
        if not name.startswith(self.name + '_'):
            raise ArcanaNameError(
                name,
                "'{}' is not explicitly provided in SubStudySpec "
                "name map and doesn't start with the SubStudySpec "
                "prefix '{}_'".format(name, self.name))
        return name[len(self.name) + 1:]

    @property
    def auto_data_specs(self):
        """
        Data specs in the sub-study class that are not explicitly provided
        in the name map
        """
        for spec in self.study_class.data_specs():
            if spec.name not in self._inv_map:
                yield spec

    @property
    def auto_option_specs(self):
        """
        Option pecs in the sub-study class that are not explicitly provided
        in the name map
        """
        for spec in self.study_class.option_specs():
            if spec.name not in self._inv_map:
                yield spec


class TranslatedPipeline(Pipeline):
    """
    A pipeline that is translated from a sub-study to the combined
    study.

    Parameters
    ----------
    name : str
        Name of the translated pipeline
    pipeline : Pipeline
        Sub-study pipeline to translate
    combined_study : MultiStudy
        Study to translate the pipeline to
    name_prefix : str
        Prefix to prepend to the pipeline name to avoid name clashes
    add_inputs : list[str]
        List of additional inputs to add to the translated pipeline
        to be connected manually in combined-study getter (i.e. not
        using translate_getter decorator).
    add_outputs : list[str]
        List of additional outputs to add to the translated pipeline
        to be connected manually in combined-study getter (i.e. not
        using translate_getter decorator).
    """

    def __init__(self, combined_study, sub_study, pipeline_name,
                 name_prefix='', add_inputs=None, add_outputs=None,
                 **kwargs):
        # Get the relative name of the sub-study (i.e. without the
        # combined study name prefixed)
        ss_name = sub_study.name[(len(combined_study.name) + 1):]
        name_prefix += ss_name + '_'
        # Create pipeline and overriding its name to include prefix
        # Copy across default options and override with extra
        # provided
        pipeline_getter = getattr(sub_study, pipeline_name)
        try:
            pipeline = pipeline_getter(name_prefix=name_prefix, **kwargs)
        except:
            raise
        try:
            assert isinstance(pipeline, Pipeline)
        except Exception:
            raise
        self._name = pipeline.name
        self._study = combined_study
        self._workflow = pipeline.workflow
        sub_study_spec = combined_study.sub_study_spec(ss_name)
        assert isinstance(pipeline.study, sub_study_spec.study_class)
        # Translate inputs from sub-study pipeline
        try:
            self._inputs = [
                i.renamed(sub_study_spec.inverse_map(i.name))
                for i in pipeline.inputs]
        except ArcanaNameError as e:
            raise ArcanaMissingDataException(
                "'{}' input required for pipeline '{}' in '{}' study "
                " is not present in inverse dataset map:\n{}".format(
                    e.name, pipeline.name, ss_name,
                    sorted(sub_study_spec.name_map.values())))
        # Add additional inputs
        self._unconnected_inputs = set()
        if add_inputs is not None:
            self._check_spec_names(add_inputs, 'additional inputs')
            self._inputs.extend(add_inputs)
            self._unconnected_inputs.update(i.name
                                            for i in add_inputs)
        # Create new input node
        self._inputnode = self.create_node(
            IdentityInterface(fields=list(self.input_names)),
            name="{}_inputnode_wrapper".format(ss_name))
        # Connect to sub-study input node
        for input_name in pipeline.input_names:
            self.workflow.connect(
                self._inputnode,
                sub_study_spec.inverse_map(input_name),
                pipeline.inputnode, input_name)
        # Translate outputs from sub-study pipeline
        self._outputs = {}
        for freq in pipeline.frequencies:
            try:
                self._outputs[freq] = [
                    o.renamed(sub_study_spec.inverse_map(o.name))
                    for o in pipeline.frequency_outputs(freq)]
            except ArcanaNameError as e:
                raise ArcanaMissingDataException(
                    "'{}' output required for pipeline '{}' in '{}' "
                    "study is not present in inverse dataset map:\n{}"
                    .format(
                        e.name, pipeline.name, ss_name,
                        sorted(sub_study_spec.name_map.values())))
        # Add additional outputs
        self._unconnected_outputs = set()
        if add_outputs is not None:
            self._check_spec_names(add_outputs, 'additional outputs')
            for output in add_outputs:
                combined_study.data_spec(output).frequency
                self._outputs[freq].append(output)
            self._unconnected_outputs.update(o.name
                                             for o in add_outputs)
        # Create output nodes for each frequency
        self._outputnodes = {}
        for freq in pipeline.frequencies:
            self._outputnodes[freq] = self.create_node(
                IdentityInterface(
                    fields=list(
                        self.frequency_output_names(freq))),
                name="{}_{}_outputnode_wrapper".format(ss_name,
                                                       freq))
            # Connect sub-study outputs
            for output_name in pipeline.frequency_output_names(freq):
                self.workflow.connect(
                    pipeline.outputnode(freq),
                    output_name,
                    self._outputnodes[freq],
                    sub_study_spec.inverse_map(output_name))
        # Copy additional info fields
        self._citations = pipeline._citations
        self._version = pipeline._version
        self._desc = pipeline._desc
        self._used_options = set()


class MultiStudyMetaClass(StudyMetaClass):
    """
    Metaclass for "multi" study classes that automatically adds
    translated data specs and pipelines from sub-study specs if they
    are not explicitly mapped in the spec.
    """

    def __new__(metacls, name, bases, dct):  # @NoSelf @UnusedVariable
        if not any(issubclass(b, MultiStudy) for b in bases):
            raise ArcanaUsageError(
                "MultiStudyMetaClass can only be used for classes that "
                "have MultiStudy as a base class")
        try:
            add_sub_study_specs = dct['add_sub_study_specs']
        except KeyError:
            add_sub_study_specs = dct['add_sub_study_specs'] = []
        try:
            add_data_specs = dct['add_data_specs']
        except KeyError:
            add_data_specs = dct['add_data_specs'] = []
        try:
            add_option_specs = dct['add_option_specs']
        except KeyError:
            add_option_specs = dct['add_option_specs'] = []
        dct['_sub_study_specs'] = sub_study_specs = {}
        for base in reversed(bases):
            try:
                add_sub_study_specs.update(
                    (d.name, d) for d in base.sub_study_specs())
            except AttributeError:
                pass
        sub_study_specs.update(
            (s.name, s) for s in add_sub_study_specs)
        explicitly_added_data_specs = [s.name for s in add_data_specs]
        explicitly_added_option_specs = [s.name
                                         for s in add_option_specs]
        # Loop through all data specs that haven't been explicitly
        # mapped and add a data spec in the multi class.
        for sub_study_spec in sub_study_specs.values():
            for data_spec in sub_study_spec.auto_data_specs:
                trans_sname = sub_study_spec.apply_prefix(
                    data_spec.name)
                if trans_sname not in explicitly_added_data_specs:
                    initkwargs = data_spec.initkwargs()
                    initkwargs['name'] = trans_sname
                    if data_spec.pipeline_name is not None:
                        trans_pname = sub_study_spec.apply_prefix(
                            data_spec.pipeline_name)
                        initkwargs['pipeline_name'] = trans_pname
                        # Check to see whether pipeline has already been
                        # translated or always existed in the class (when
                        # overriding default options for example)
                        if trans_pname not in dct:
                            dct[trans_pname] = MultiStudy.translate(
                                sub_study_spec.name,
                                data_spec.pipeline_name)
                    add_data_specs.append(type(data_spec)(**initkwargs))
            for opt_spec in sub_study_spec.auto_option_specs:
                trans_sname = sub_study_spec.apply_prefix(
                    opt_spec.name)
                if trans_sname not in explicitly_added_option_specs:
                    add_option_specs.append(
                        opt_spec.renamed(trans_sname))
        cls = StudyMetaClass(name, bases, dct)
        # Check all names in name-map correspond to data or option
        # specs
        for sub_study_spec in sub_study_specs.values():
            local_spec_names = list(
                sub_study_spec.study_class.spec_names())
            for global_name, local_name in sub_study_spec._name_map.items():
                if local_name not in local_spec_names:
                    raise ArcanaUsageError(
                        "'{}' in name-map for '{}' sub study spec in {}"
                        "MultiStudy class does not name a data or "
                        "option spec in {} class"
                        .format(local_name, sub_study_spec.name,
                                name, sub_study_spec.study_class))
                if global_name not in cls.spec_names():
                    raise ArcanaUsageError(
                        "'{}' in name-map for '{}' sub study spec in {}"
                        "MultiStudy class does not name data or option spec"
                        .format(global_name, sub_study_spec.name, name))
        return cls
