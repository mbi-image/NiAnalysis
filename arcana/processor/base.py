from builtins import object
import os  # @UnusedImport
from pprint import pformat
import os.path as op
from collections import defaultdict, OrderedDict
import shutil
from itertools import zip_longest
from copy import copy, deepcopy
from logging import getLogger
import numpy as np
from nipype.pipeline import engine as pe
from nipype.interfaces.utility import IdentityInterface, Merge
from arcana.pipeline import Pipeline
from arcana.repository.interfaces import RepositorySource, RepositorySink
from arcana.utils import get_class_info
from arcana.exceptions import (
    ArcanaMissingDataException,
    ArcanaNoRunRequiredException, ArcanaUsageError, ArcanaDesignError,
    ArcanaProvenanceRecordMismatchError, ArcanaProtectedOutputConflictError,
    ArcanaOutputNotProducedException)


logger = getLogger('arcana')


WORKFLOW_MAX_NAME_LEN = 100


class BaseProcessor(object):
    """
    A thin wrapper around the NiPype LinearPlugin used to connect
    runs pipelines on the local workstation

    Parameters
    ----------
    work_dir : str
        A directory in which to run the nipype workflows
    max_process_time : float
        The maximum time allowed for the process
    reprocess: bool
        A flag which determines whether to rerun the processing for this
        step if a provenance mismatch is detected between save derivative and
        parameters passed to the Study. If False, an exception will be raised
        in this case
    prov_include : iterable[str]
        Paths in the provenance dictionary to include in checks with previously
        generated derivatives to determine whether they need to be rerun.
        Paths are strings delimited by '/', with each part referring to a
        dictionary key in the nested provenance dictionary.
    prov_exclude : iterable[str]
        Paths in the provenance dictionary to exclude (if they are already
        included given the 'prov_include' kwarg) in checks with previously
        generated derivatives to determine whether they need to be rerun
        Paths are strings delimited by '/', with each part referring to a
        dictionary key in the nested provenance dictionary.
    clean_work_dir_between_runs : bool
        Whether to clean the working directory between runs (can avoid problems
        if debugging the analysis but may take longer to reach the same point)
    default_wall_time : int
        The default wall time assumed for nodes where it isn't specified
    default_mem_gb : float
        The default memory assumed to be required for nodes where it isn't
        specified
    """

    DEFAULT_WALL_TIME = 20
    DEFAULT_MEM_GB = 4096
    DEFAULT_PROV_INCLUDE = ['workflow', 'study/subject_ids', 'study/visit_ids',
                            'inputs', 'outputs']
    DEFAULT_PROV_EXCLUDE = ['workflow/nodes/interface/.*/pkg_version']

    default_plugin_args = {}

    def __init__(self, work_dir, reprocess=False,
                 prov_include=DEFAULT_PROV_INCLUDE,
                 prov_exclude=DEFAULT_PROV_EXCLUDE,
                 max_process_time=None,
                 clean_work_dir_between_runs=True,
                 default_wall_time=DEFAULT_WALL_TIME,
                 default_mem_gb=DEFAULT_MEM_GB, **kwargs):
        self._work_dir = work_dir
        self._max_process_time = max_process_time
        self._reprocess = reprocess
        self._prov_include = prov_include
        self._prov_exclude = prov_exclude
        self._plugin_args = copy(self.default_plugin_args)
        self._default_wall_time = default_wall_time
        self._deffault_mem_gb = default_mem_gb
        self._plugin_args.update(kwargs)
        self._init_plugin()
        self._study = None
        self._clean_work_dir_between_runs = clean_work_dir_between_runs

    def __repr__(self):
        return "{}(work_dir={})".format(
            type(self).__name__, self._work_dir)

    def __eq__(self, other):
        try:
            return (
                self._work_dir == other._work_dir and
                self._max_process_time == other._max_process_time and
                self._reprocess == other._reprocess and
                self._plugin_args == other._plugin_args)
        except AttributeError:
            return False

    def _init_plugin(self):
        self._plugin = self.nipype_plugin_cls(**self._plugin_args)

    @property
    def study(self):
        return self._study

    @property
    def reprocess(self):
        return self._reprocess

    @property
    def prov_include(self):
        return self._prov_include

    @property
    def prov_exclude(self):
        return self._prov_exclude

    @property
    def default_mem_gb(self):
        return self._deffault_mem_gb

    @property
    def default_wall_time(self):
        return self._default_wall_time

    def bind(self, study):
        cpy = deepcopy(self)
        cpy._study = study
        return cpy

    def run(self, *pipelines, **kwargs):
        """
        Connects all pipelines to that study's repository and runs them
        in the same NiPype workflow

        Parameters
        ----------
        pipeline(s) : Pipeline, ...
            The pipeline to connect to repository
        required_outputs : list[set[str]]
            A set of required outputs for each pipeline. If None then all
            outputs are assumed to be required
        subject_ids : list[str]
            The subset of subject IDs to process. If None all available will be
            processed. Note this is not a duplication of the study
            and visit IDs passed to the Study __init__, as they define the
            scope of the analysis and these simply limit the scope of the
            current run (e.g. to break the analysis into smaller chunks and
            run separately). Therefore, if the analysis joins over subjects,
            then all subjects will be processed and this parameter will be
            ignored.
        visit_ids : list[str]
            The same as 'subject_ids' but for visit IDs
        session_ids : list[str,str]
            The same as 'subject_ids' and 'visit_ids', except specifies a set
            of specific combinations in tuples of (subject ID, visit ID).
        force : bool | 'all'
            A flag to force the reprocessing of all sessions in the filter
            array, regardless of whether the parameters|pipeline used
            to generate them matches the current ones. NB: if True only the
            final pipeline will be reprocessed (prerequisite pipelines won't
            run unless they don't match provenance). To process all
            prerequisite pipelines 'all' should be passed to force.

        Returns
        -------
        report : ReportNode
            The final report node, which can be connected to subsequent
            pipelines
        """
        if not pipelines:
            raise ArcanaUsageError("No pipelines provided to {}.run"
                                   .format(self))
        # Get filter kwargs  (NB: in Python 3 they could be in the kwarg list)
        subject_ids = kwargs.pop('subject_ids', None)
        visit_ids = kwargs.pop('visit_ids', None)
        session_ids = kwargs.pop('session_ids', None)
        clean_work_dir = kwargs.pop('clean_work_dir',
                                    self._clean_work_dir_between_runs)
        required_outputs = kwargs.pop('required_outputs', None)
        # Create name by combining pipelines
        name = '_'.join(p.name for p in pipelines)
        # Clean work dir if required
        if clean_work_dir:
            workflow_work_dir = op.join(self.work_dir, name)
            if op.exists(workflow_work_dir):
                shutil.rmtree(workflow_work_dir)
        # Trim the end of very large names to avoid problems with
        # workflow names exceeding system limits.
        name = name[:WORKFLOW_MAX_NAME_LEN]
        workflow = pe.Workflow(name=name, base_dir=self.work_dir)

        # Generate filter array to optionally restrict the run to certain
        # subject and visit IDs.
        tree = self.study.tree
        # Create maps from the subject|visit IDs to an index used to represent
        # them in the filter array
        subject_inds = {s.id: i for i, s in enumerate(tree.subjects)}
        visit_inds = {v.id: i for i, v in enumerate(tree.visits)}
        if not subject_ids and not visit_ids and not session_ids:
            # No filters applied so create a full filter array
            filter_array = np.ones((len(subject_inds), len(visit_inds)),
                                   dtype=bool)
        else:
            # Filters applied so create an empty filter array and populate
            # from filter lists
            filter_array = np.zeros((len(subject_inds), len(visit_inds)),
                                    dtype=bool)
            if subject_ids is not None:
                for subj_id in subject_ids:
                    filter_array[subject_inds[subj_id], :] = True
            if visit_ids is not None:
                for visit_id in visit_ids:
                    filter_array[:, visit_inds[visit_id]] = True
            if session_ids is not None:
                for subj_id, visit_id in session_ids:
                    filter_array[subject_inds[subj_id],
                                 visit_inds[visit_id]] = True
            if not filter_array.any():
                raise ArcanaUsageError(
                    "Provided filters:\n" +
                    ("  subject_ids: {}\n".format(', '.join(subject_ids))
                     if subject_ids is not None else '') +
                    ("  visit_ids: {}\n".format(', '.join(visit_ids))
                     if visit_ids is not None else '') +
                    ("  session_ids: {}\n".format(', '.join(session_ids))
                     if session_ids is not None else '') +
                    "Did not match any sessions in the project:\n" +
                    "  subject_ids: {}\n".format(', '.join(subject_inds)) +
                    "  visit_ids: {}\n".format(', '.join(visit_inds)))

        # Stack of pipelines to process in reverse order of required execution
        stack = OrderedDict()

        def push_on_stack(pipeline, filt_array, study=None, req_outputs=None):
            if isinstance(pipeline, Pipeline):
                # If a primary pipeline (i.e. one passed to this run method
                # explicitly
                try:
                    pipeline_name = pipeline._getter_name  # To match prereqs
                except AttributeError:
                    pipeline_name = pipeline.name
                study = pipeline.study
            else:
                # If a prerequisite referenced by name
                pipeline_name = pipeline
                pipeline = None
            key = (id(study), pipeline_name)
            if key in stack:
                # Pop pipeline from stack in order to add it to the end of the
                # stack and ensure it is run before all downstream pipelines
                pipeline, prev_req_outputs, prev_filt_array = stack.pop(key)
                # Combined required outputs
                req_outputs = copy(req_outputs)
                req_outputs.update(prev_req_outputs)
                filt_array = filt_array | prev_filt_array
            elif pipeline is None:
                pipeline = study.get_pipeline(pipeline_name)
            # Check that the required outputs are created with the given
            # parameters
            missing_outputs = req_outputs - set(pipeline.output_names)
            if missing_outputs:
                raise ArcanaOutputNotProducedException(
                    "Output(s) '{}', required for {}, will "
                    "not be created by prerequisite pipeline '{}' "
                    "with parameters: {}".format(
                        "', '".join(missing_outputs), pipeline._error_msg_loc,
                        pipeline.name,
                        '\n'.join('{}={}'.format(o.name, o.value)
                                  for o in study.parameters)))
            # If the pipeline to process contains summary outputs (i.e. 'per-
            # subject|visit|study' frequency), then we need to "dialate" the
            # filter array to include IDs across the scope of the study, e.g.
            # all subjects for per-vist, or all visits for per-subject.
            output_freqs = set(pipeline.output_frequencies)
            dialated_filt_array = self._dialate_array(filt_array, output_freqs)
            added = dialated_filt_array ^ filt_array
            if added.any():
                filt_array = dialated_filt_array
                # Invert the index dictionaries to get index-to-ID maps
                inv_subject_inds = {v: k for k, v in subject_inds.items()}
                inv_visit_inds = {v: k for k, v in visit_inds.items()}
                logger.warning(
                    "Dialated filter array used to process '{}' pipeline to "
                    "include {} subject/visit IDs due to its '{}' summary "
                    "outputs ".format(
                        pipeline.name,
                        ', '.join('({},{})'.format(inv_subject_inds[s],
                                                   inv_visit_inds[v])
                                  for s, v in zip(*np.nonzero(added))),
                        "' and '".join(output_freqs)))
            # Append pipeline to stack
            if pipeline.name in [s[0].name for s in stack.values()]:
                raise ArcanaDesignError(
                    "Attempting to run muliple pipelines with the same name "
                    "('{}') in the same workflow"
                    .format(pipeline.name))
            stack[key] = pipeline, req_outputs, filt_array
            # Recursively add all prerequisites to stack
            for prq_name, prq_req_outputs in pipeline.prerequisites.items():
                try:
                    push_on_stack(prq_name, filt_array, study=study,
                                  req_outputs=prq_req_outputs)
                except ArcanaMissingDataException as e:
                    raise ArcanaMissingDataException(
                        "{}, which is required as an input of the '{}' "
                        "pipeline to produce '{}'"
                        .format(e, self.name, "', '".join(req_outputs)))

        # Add all primary pipelines to the stack along with their prereqs
        for pipeline, req_outputs in zip_longest(pipelines, required_outputs):
            push_on_stack(pipeline, filter_array, req_outputs=req_outputs)

        # Iterate through stack of required pipelines from upstream to
        # downstream
        connected_pipelines = {}
        for key, (pipeline,
                  req_outputs, flt_array) in reversed(list(stack.items())):
            try:
                connected_pipelines[key] = self._connect_pipeline(
                    pipeline, req_outputs, connected_pipelines, workflow,
                    subject_inds, visit_inds, flt_array, **kwargs)
            except ArcanaNoRunRequiredException:
                logger.info("Not running '{}' pipeline as its outputs "
                            "are already present in the repository"
                            .format(pipeline.name))
                connected_pipelines[key] = None
#         workflow.write_graph(graph2use='flat', format='svg')
#         print('Graph saved in {} directory'.format(os.getcwd()))
        # Actually run the generated workflow
        result = workflow.run(plugin=self._plugin)
        # Reset the cached tree of filesets in the repository as it will
        # change after the pipeline has run.
        self.study.clear_cache()
        return result

    def _connect_pipeline(self, pipeline, required_outputs,
                          connected_pipelines, workflow, subject_inds,
                          visit_inds, filter_array, force=False):
        """
        Connects a pipeline to a overarching workflow that sets up iterators
        over subjects|visits present in the repository (if required) and
        repository source and sink nodes

        Parameters
        ----------
        pipeline : Pipeline
            The pipeline to connect
        required_outputs : set[str] | None
            The outputs required to be produced by this pipeline. If None all
            are deemed to be required
        connected_pipelines : dict[str, Pipeline]
            A dictionary containing all pipelines that have already been
            connected to avoid the same pipeline being connected twice.
        workflow : nipype.pipeline.engine.Workflow
            The overarching workflow to connect the pipeline to
        subject_inds : dct[str, int]
            A mapping of subject ID to row index in the filter array
        visit_inds : dct[str, int]
            A mapping of visit ID to column index in the filter array
        filter_array : 2-D numpy.array[bool]
            A two-dimensional boolean array, where rows correspond to
            subjects and columns correspond to visits in the repository. True
            values represent a combination of subject & visit ID to include
            in the current round of processing. Note that if the 'force'
            flag is not set, sessions won't be reprocessed unless the
            save provenance doesn't match that of the given pipeline.
        force : bool | 'all'
            A flag to force the processing of all sessions in the filter
            array, regardless of whether the parameters|pipeline used
            to generate existing data matches the given pipeline
        """
        # Close-off construction of the pipeline and created, input and output
        # nodes and provenance dictionary
        pipeline.cap()
        # Prepend prerequisite pipelines to complete workflow if they need
        # to be (re)processed
        final_nodes = []
        # The array that represents the subject/visit pairs for which any
        # prerequisite pipeline will be (re)processed, and which therefore
        # needs to be included in the processing of the current pipeline. Row
        # indices correspond to subjects and column indices visits
        prqs_to_process_array = np.zeros((len(subject_inds), len(visit_inds)),
                                         dtype=bool)
        for prq_name in pipeline.prerequisites:
            prereq = connected_pipelines[(id(pipeline.study), prq_name)]
            if prereq is not None:  # If prerequisite needs to be run
                final_node, prq_to_process_array = prereq
                prqs_to_process_array |= prq_to_process_array
                final_nodes.append(final_node)
        # Get list of sessions that need to be processed (i.e. if
        # they don't contain the outputs of this pipeline)
        to_process_array = self._to_process(
            pipeline, required_outputs, prqs_to_process_array, filter_array,
            subject_inds, visit_inds, force)
        # Check to see if there are any sessions to process
        if not to_process_array.any():
            raise ArcanaNoRunRequiredException(
                "No sessions to process for '{}' pipeline"
                .format(pipeline.name))
        # Set up workflow to run the pipeline, loading and saving from the
        # repository
        workflow.add_nodes([pipeline._workflow])
        # If prerequisite pipelines need to be processed, connect their
        # "final" nodes to the initial node of this pipeline to ensure that
        # they are all processed before this pipeline is run.
        if final_nodes:
            prereqs = pipeline.add('prereqs', Merge(len(final_nodes)))
            for i, final_node in enumerate(final_nodes, start=1):
                workflow.connect(final_node, 'out', prereqs, 'in{}'.format(i))
        else:
            prereqs = None
        # Construct iterator structure over subjects and sessions to be
        # processed
        iter_nodes = self._iterate(pipeline, to_process_array, subject_inds,
                                   visit_inds)
        sources = {}
        # Loop through each frequency present in the pipeline inputs and
        # create a corresponding source node
        for freq in pipeline.input_frequencies:
            try:
                inputs = list(pipeline.frequency_inputs(freq))
            except ArcanaMissingDataException as e:
                raise ArcanaMissingDataException(
                    str(e) + ", which is required for pipeline '{}'".format(
                        pipeline.name))
            inputnode = pipeline.inputnode(freq)
            sources[freq] = source = pipeline.add(
                '{}_source'.format(freq),
                RepositorySource(
                    i.collection for i in inputs),
                connect=({'prereqs': (prereqs, 'out')} if prereqs is not None
                         else {}))
            # Connect iter_nodes to source and input nodes
            for iterator in pipeline.iterators(freq):
                pipeline.connect(iter_nodes[iterator], iterator, source,
                                 iterator)
                pipeline.connect(source, iterator, inputnode,
                                 iterator)
            for input in inputs:  # @ReservedAssignment
                pipeline.connect(source, input.suffixed_name,
                                 inputnode, input.name)
        deiter_nodes = {}

        def deiter_node_sort_key(it):
            """
            If there are two iter_nodes (i.e. both subject and visit ID) and
            one depends on the other (i.e. if the visit IDs per subject
            vary and vice-versa) we need to ensure that the dependent
            iterator is deiterated (joined) first.
            """
            return iter_nodes[it].itersource is None

        # Connect all outputs to the repository sink, creating a new sink for
        # each frequency level (i.e 'per_session', 'per_subject', 'per_visit',
        # or 'per_study')
        for freq in pipeline.output_frequencies:
            outputs = list(pipeline.frequency_outputs(freq))
            if pipeline.iterators(freq) - pipeline.iterators():
                raise ArcanaDesignError(
                    "Doesn't make sense to output '{}', which are of '{}' "
                    "frequency, when the pipeline only iterates over '{}'"
                    .format("', '".join(o.name for o in outputs), freq,
                            "', '".join(pipeline.iterators())))
            outputnode = pipeline.outputnode(freq)
            # Connect filesets/fields to sink to sink node, skipping outputs
            # that are study inputs
            to_connect = {o.suffixed_name: (outputnode, o.name)
                          for o in outputs if o.is_spec}
            # Connect iterators to sink node
            to_connect.update(
                {i: (iter_nodes[i], i) for i in pipeline.iterators()})
            # Connect checksums/values from sources to sink node in order to
            # save in provenance, joining where necessary
            for input_freq in pipeline.input_frequencies:
                checksums_to_connect = [
                    i.checksum_suffixed_name
                    for i in pipeline.frequency_inputs(input_freq)]
                if not checksums_to_connect:
                    # Rare case of a pipeline with no inputs only iter_nodes
                    # that will only occur in unittests in all likelihood
                    continue
                # Loop over iterators that need to be joined, i.e. that are
                # present in the input frequency but not the output frequency
                # and create join nodes
                source = sources[input_freq]
                for iterator in (pipeline.iterators(input_freq) -
                                  pipeline.iterators(freq)):
                    join = pipeline.add(
                        '{}_to_{}_{}_checksum_join'.format(
                            input_freq, freq, iterator),
                        IdentityInterface(
                            checksums_to_connect),
                        connect={
                            tc: (source, tc) for tc in checksums_to_connect},
                        joinsource=iterator,
                        joinfield=checksums_to_connect)
                    source = join
                to_connect.update(
                    {tc: (source, tc) for tc in checksums_to_connect})
            # Add sink node
            sink = pipeline.add(
                '{}_sink'.format(freq),
                RepositorySink(
                    (o.collection for o in outputs), pipeline),
                connect=to_connect)
            # "De-iterate" (join) over iterators to get back to single child
            # node by the time we connect to the final node of the pipeline Set
            # the sink and subject_id as the default deiterator if there are no
            # deiterates (i.e. per_study) or to use as the upstream node to
            # connect the first deiterator for every frequency
            deiter_nodes[freq] = sink  # for per_study the "deiterator" == sink
            for iterator in sorted(pipeline.iterators(freq),
                                    key=deiter_node_sort_key):
                # Connect to previous deiterator or sink
                # NB: we only need to keep a reference to the last one in the
                # chain in order to connect with the "final" node, so we can
                # overwrite the entry in the 'deiter_nodes' dict
                deiter_nodes[freq] = pipeline.add(
                    '{}_{}_deiter'.format(freq, iterator),
                    IdentityInterface(
                        ['checksums']),
                    connect={
                        'checksums': (deiter_nodes[freq], 'checksums')},
                    joinsource=iterator,
                    joinfield='checksums')
        # Create a final node, which is used to connect with dependent
        # pipelines into large workflows
        final = pipeline.add(
            'final',
            Merge(
                len(deiter_nodes)),
            connect={
                'in{}'.format(i): (di, 'checksums')
                for i, di in enumerate(deiter_nodes.values(), start=1)})
        return final, to_process_array

    def _iterate(self, pipeline, to_process_array, subject_inds, visit_inds):
        """
        Generate nodes that iterate over subjects and visits in the study that
        need to be processed by the pipeline

        Parameters
        ----------
        pipeline : Pipeline
            The pipeline to add iter_nodes for
        to_process_array : 2-D numpy.array[bool]
            A two-dimensional boolean array, where rows correspond to
            subjects and columns correspond to visits in the repository. True
            values represent a combination of subject & visit ID to process
            the session for
        subject_inds : dct[str, int]
            A mapping of subject ID to row index in the 'to_process' array
        visit_inds : dct[str, int]
            A mapping of visit ID to column index in the 'to_process' array

        Returns
        -------
        iter_nodes : dict[str, Node]
            A dictionary containing the nodes to iterate over all subject/visit
            IDs to process for each input frequency
        """
        # Check to see whether the subject/visit IDs to process (as specified
        # by the 'to_process' array) can be factorized into indepdent nodes,
        # i.e. all subjects to process have the same visits to process and
        # vice-versa.
        factorizable = True
        if len(list(pipeline.iterators())) == 2:
            nz_rows = to_process_array[to_process_array.any(axis=1), :]
            ref_row = nz_rows[0, :]
            factorizable = all((r == ref_row).all() for r in nz_rows)
        # If the subject/visit IDs to process cannot be factorized into
        # indepedent iterators, determine which to make make dependent on the
        # other in order to avoid/minimise duplicatation of download attempts
        dependent = None
        if not factorizable:
            input_freqs = list(pipeline.input_frequencies)
            # By default pick iterator the one with the most IDs to
            # iterate to be the dependent in order to reduce the number of
            # nodes created and any duplication of download attempts across
            # the nodes (if both 'per_visit' and 'per_subject' inputs are
            # required
            num_subjs, num_visits = nz_rows[:, nz_rows.any(axis=0)].shape
            if num_subjs > num_visits:
                dependent = self.study.SUBJECT_ID
            else:
                dependent = self.study.VISIT_ID
            if 'per_visit' in input_freqs:
                if 'per_subject' in input_freqs:
                    logger.warning(
                        "Cannot factorize sessions to process into independent"
                        " subject and visit iterators and both 'per_visit' and"
                        " 'per_subject' inputs are used by pipeline therefore"
                        " per_{} inputs may be cached twice".format(
                            dependent[:-3]))
                else:
                    dependent = self.study.SUBJECT_ID
            elif 'per_subject' in input_freqs:
                dependent = self.study.VISIT_ID
        # Invert the index dictionaries to get index-to-ID maps
        inv_subj_inds = {v: k for k, v in subject_inds.items()}
        inv_visit_inds = {v: k for k, v in visit_inds.items()}
        # Create iterator for subjects
        iter_nodes = {}
        if self.study.SUBJECT_ID in pipeline.iterators():
            fields = [self.study.SUBJECT_ID]
            if dependent == self.study.SUBJECT_ID:
                fields.append(self.study.VISIT_ID)
            # Add iterator node named after subject iterator
            subj_it = pipeline.add(self.study.SUBJECT_ID,
                                   IdentityInterface(fields))
            if dependent == self.study.SUBJECT_ID:
                # Subjects iterator is dependent on visit iterator (because of
                # non-factorizable IDs)
                subj_it.itersource = ('{}_{}'.format(pipeline.name,
                                                     self.study.VISIT_ID),
                                      self.study.VISIT_ID)
                subj_it.iterables = [(
                    self.study.SUBJECT_ID,
                    {inv_visit_inds[n]: [inv_subj_inds[m]
                                         for m in col.nonzero()[0]]
                     for n, col in enumerate(to_process_array.T)})]
            else:
                subj_it.iterables = (
                    self.study.SUBJECT_ID,
                    [inv_subj_inds[n]
                     for n in to_process_array.any(axis=1).nonzero()[0]])
            iter_nodes[self.study.SUBJECT_ID] = subj_it
        # Create iterator for visits
        if self.study.VISIT_ID in pipeline.iterators():
            fields = [self.study.VISIT_ID]
            if dependent == self.study.VISIT_ID:
                fields.append(self.study.SUBJECT_ID)
            # Add iterator node named after visit iterator
            visit_it = pipeline.add(self.study.VISIT_ID,
                                    IdentityInterface(fields))
            if dependent == self.study.VISIT_ID:
                visit_it.itersource = ('{}_{}'.format(pipeline.name,
                                                      self.study.SUBJECT_ID),
                                       self.study.SUBJECT_ID)
                visit_it.iterables = [(
                    self.study.VISIT_ID,
                    {inv_subj_inds[m]:[inv_visit_inds[n]
                                       for n in row.nonzero()[0]]
                     for m, row in enumerate(to_process_array)})]
            else:
                visit_it.iterables = (
                    self.study.VISIT_ID,
                    [inv_visit_inds[n]
                     for n in to_process_array.any(axis=0).nonzero()[0]])
            iter_nodes[self.study.VISIT_ID] = visit_it
        if dependent == self.study.SUBJECT_ID:
            pipeline.connect(visit_it, self.study.VISIT_ID,
                             subj_it, self.study.VISIT_ID)
        if dependent == self.study.VISIT_ID:
            pipeline.connect(subj_it, self.study.SUBJECT_ID,
                             visit_it, self.study.SUBJECT_ID)
        return iter_nodes

    def _to_process(self, pipeline, required_outputs, prqs_to_process_array,
                    filter_array, subject_inds, visit_inds, force):
        """
        Check whether the outputs of the pipeline are present in all sessions
        in the project repository and were generated with matching provenance.
        Return an 2D boolean array (subjects: rows, visits: cols) with the
        sessions to process marked True.

        Parameters
        ----------
        pipeline : Pipeline
            The pipeline to determine the sessions to process
        required_ouputs : set[str]
            The names of the pipeline outputs that are required. If None all
            are deemed to be required
        prqs_to_process_array : 2-D numpy.array[bool]
            A two-dimensional boolean array, where rows and columns correspond
            correspond to subjects and visits in the repository tree. True
            values represent a subject/visit ID pairs that will be
            (re)processed in prerequisite pipelines and therefore need to be
            included in the returned array.
        filter_array : 2-D numpy.array[bool]
            A two-dimensional boolean array, where rows and columns correspond
            correspond to subjects and visits in the repository tree. True
            values represent a subject/visit ID pairs to include
            in the current round of processing. Note that if the 'force'
            flag is not set, sessions won't be reprocessed unless the
            parameters and pipeline version saved in the provenance doesn't
            match that of the given pipeline.
        subject_inds : dict[str,int]
            Mapping from subject ID to index in filter|to_process arrays
        visit_inds : dict[str,int]
            Mapping from visit ID to index in filter|to_process arrays
        force : bool
            Whether to force reprocessing of all (filtered) sessions or not.
            Note that if 'force' is true we can't just return the filter array
            as it might be dilated by summary outputs (i.e. of frequency
            'per_visit', 'per_subject' or 'per_study'). So we still loop
            through all outputs and treat them like they don't exist

        Returns
        -------
        to_process_array : 2-D numpy.array[bool]
            A two-dimensional boolean array, where rows correspond to
            subjects and columns correspond to visits in the repository. True
            values represent subject/visit ID pairs to run the pipeline for
        """
        # Reference the study tree in local variable for convenience
        tree = self.study.tree
        # Check to see if the pipeline has any low frequency outputs, because
        # if not then each session can be processed indepdently. Otherwise,
        # the "session matrix" (as defined by subject_ids and visit_ids
        # passed to the Study class) needs to be complete, i.e. a session
        # exists (with the full complement of requird inputs) for each
        # subject/visit ID pair.
        summary_outputs = [
            o.name for o in pipeline.outputs if o.frequency != 'per_session']
        # Set of frequencies present in pipeline outputs
        output_freqs = pipeline.output_frequencies
        if summary_outputs:
            if list(tree.incomplete_subjects):
                raise ArcanaUsageError(
                    "Can't process '{}' pipeline as it has low frequency "
                    " outputs (i.e. outputs that aren't of 'per_session' "
                    "frequency) ({}) and subjects ({}) that are missing one "
                    "or more visits ({}). Please restrict the subject/visit "
                    "IDs in the study __init__ to continue the analysis"
                    .format(
                        self.name,
                        ', '.join(summary_outputs),
                        ', '.join(s.id for s in tree.incomplete_subjects),
                        ', '.join(v.id for v in tree.incomplete_visits)))
        # Initalise array to represent which sessions need to be reprocessed
        to_process_array = np.zeros((len(subject_inds), len(visit_inds)),
                                    dtype=bool)
        # An array to mark outputs that have been altered outside of Arcana
        # and therefore protect from over-writing
        to_protect_array = np.copy(to_process_array)
        # As well as the the sessions that need to be protected, keep track
        # of the items in those sessions that need to be protected for more
        # informative warnings/errors
        to_protect = defaultdict(list)
        # Mark the sessions that we should check to see if the configuration
        # saved in the provenance record matches that of the current pipeline
        to_check_array = np.copy(to_process_array)
        # Check for sessions for missing required outputs
        for output in pipeline.outputs:
            # Check to see if output is required by downstream processing
            required = (required_outputs is None or
                        output.name in required_outputs)
            for item in output.collection:
                # Get row and column indices, if low-frequency (e.g.
                # per_subject/visit/study) then just mark the first cell in
                # row|column as it will be "dialated" afterwards
                inds = (subject_inds.get(item.subject_id, 0),
                        visit_inds.get(item.visit_id, 0))
                if item.exists:
                    # Check to see if checksums recorded when derivative
                    # was generated by previous run match those of current file
                    # set. If not we assume they have been manually altered and
                    # therefore should not be overridden
                    if item.checksums != item.recorded_checksums:
                        logger.warning(
                            "Checksums for {} do not match those recorded in "
                            "provenance. Assuming it has been manually "
                            "corrected outside of Arcana and will therefore "
                            "not overwrite. Please delete manually if this "
                            "is not intended".format(repr(item)))
                        to_protect_array[inds] = True
                        to_protect[inds].append(item)
                    elif force and required:
                        to_process_array[inds] = True
                    else:
                        to_check_array[inds] = True
                elif required:
                    to_process_array[inds] = True
        # Filter sessions to process by those requested
        to_process_array *= filter_array
        to_check_array *= filter_array
        if to_check_array.any() and self.prov_include:
            # Get list of sessions, subjects, visits, tree objects to check
            # their provenance against that of the pipeline
            to_check = []
            if 'per_session' in output_freqs:
                to_check.extend(
                    s for s in tree.sessions
                    if to_check_array[subject_inds[s.subject_id],
                                      visit_inds[s.visit_id]])
            if 'per_subject' in output_freqs:
                # We can just test the first col of outputs_exist as rows
                # should be either all True or all False
                to_check.extend(s for s in tree.subjects
                                if to_check_array[subject_inds[s.id], 0])
            if 'per_visit' in output_freqs:
                # We can just test the first row of outputs_exist as cols
                # should be either all True or all False
                to_check.extend(v for v in tree.visits
                                if to_check_array[0, visit_inds[v.id]])
            if 'per_study' in output_freqs:
                to_check.append(tree)
            for node in to_check:
                # Retrieve record stored in tree node
                record = node.record(pipeline.name, pipeline.study.name)
                # Generated expected record from current pipeline/repository-
                # state
                expected_record = pipeline.expected_record(node)
                # Compare record with expected
                mismatches = record.mismatches(expected_record,
                                               self.prov_include,
                                               self.prov_exclude)
                if mismatches:
                    if self.reprocess:
                        to_process_array[
                            subject_inds.get(node.subject_id, 0),
                            visit_inds.get(node.visit_id, 0)] = True
                        logger.info(
                            "Reprocessing {} with '{}' "
                            "pipeline due to mismatching provenance:\n{}"
                            .format(node, pipeline.name, mismatches))
                    else:
                        raise ArcanaProvenanceRecordMismatchError(
                            "Provenance recorded for '{}' pipeline in {} does "
                            "not match that of requested pipeline, set "
                            "reprocess flag == True to overwrite:\n{}".format(
                                pipeline.name, self, mismatches))
        # Dialate to process array
        to_process_array = self._dialate_array(to_process_array, output_freqs)
        # Check for conflicts between nodes to process and nodes to protect
        conflicting = to_process_array * to_protect_array
        if conflicting.any():
            error_msg = ''
            for sess_inds in zip(*np.nonzero(conflicting)):
                subject_id = next(k for k, v in subject_inds.items()
                                  if v == sess_inds[0])
                visit_id = next(k for k, v in visit_inds.items()
                                if v == sess_inds[1])
                if required_outputs is None:
                    conflict_outputs = pipeline.outputs
                else:
                    conflict_outputs = [pipeline.study.bound_spec(r)
                                        for r in required_outputs]
                items = [
                    o.collection.item(subject_id=subject_id, visit_id=visit_id)
                    for o in conflict_outputs]
                missing = [i for i in items if i not in to_protect[sess_inds]]
                error_msg += (
                    "\n({}, {}): protected=[{}], missing=[{}]"
                    .format(
                        subject_id, visit_id,
                        ', '.join(repr(i) for i in to_protect[sess_inds]),
                        ', '.join(repr(i) for i in missing)))
            raise ArcanaProtectedOutputConflictError(
                "Cannot process {} as there are nodes with both protected "
                "outputs (ones modified externally to Arcana) and missing "
                "required outputs. Either delete protected outputs or provide "
                "missing required outputs to continue:{}".format(pipeline,
                                                                 error_msg))
        # Add in any prerequisites to process that aren't explicitly protected
        to_process_array |= (prqs_to_process_array *
                             filter_array *
                             np.invert(to_protect_array))
        to_process_array = self._dialate_array(to_process_array, output_freqs)
        return to_process_array

    def _dialate_array(self, array, output_freqs):
        """
        'Dialates' an array so all subject/visit ID cells required by
        low frequency outputs (i.e. all subjects per-visit for
        'per_visit', all visits per-subject for 'per_subject', all
        for 'per_study') are included in the array if any need for that
        subject/visit need to be processed.
        """
        output_freqs = set(output_freqs)
        if output_freqs == set(
                ['per_session']) or array.all() or not array.any():
            return array
        dialated = np.copy(array)
        if 'per_study' in output_freqs:
            dialated[:, :] = True
        elif 'per_subject' in output_freqs:
            dialated[dialated.any(axis=1), :] = True
        elif 'per_visit' in output_freqs:
            dialated[:, dialated.any(axis=0)] = True
        return dialated

    @property
    def work_dir(self):
        return self._work_dir

    def __getstate__(self):
        dct = copy(self.__dict__)
        # Delete the NiPype plugin as it can be regenerated
        del dct['_plugin']
        return dct

    def __setstate__(self, state):
        self.__dict__.update(state)
        self._init_plugin()

    @property
    def prov(self):
        return {'type': get_class_info(type(self))}