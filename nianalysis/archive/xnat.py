from __future__ import absolute_import
from abc import ABCMeta
import os.path
from itertools import repeat
import shutil
import hashlib
import stat
import time
import logging
import errno
import json
from zipfile import ZipFile
from collections import defaultdict
from nipype.pipeline import engine as pe
from nipype.interfaces.base import Directory, traits, isdefined
from nianalysis.dataset import Dataset, Field
from nianalysis.archive.base import (
    Archive, ArchiveSource, ArchiveSink, ArchiveSourceInputSpec,
    ArchiveSinkInputSpec, ArchiveSubjectSinkInputSpec,
    ArchiveVisitSinkInputSpec,
    ArchiveProjectSinkInputSpec, Session, Subject, Project, Visit,
    ArchiveSubjectSink, ArchiveVisitSink, ArchiveProjectSink,
    MULTIPLICITIES)
from nianalysis.interfaces.iterators import (
    InputSessions, InputSubjects)
from nianalysis.data_formats import data_formats
from nianalysis.utils import split_extension
from nianalysis.exceptions import (
    NiAnalysisError, NiAnalysisXnatArchiveMissingDatasetException)
from nianalysis.utils import dir_modtime
import re
import xnat  # NB: XNATPy not PyXNAT
from nianalysis.utils import PATH_SUFFIX, FIELD_SUFFIX

logger = logging.getLogger('NiAnalysis')

special_char_re = re.compile(r'[^a-zA-Z_0-9]')


class XNATMixin(object):

    @property
    def session_id(self):
        return self.inputs.subject_id + '_' + self.inputs.visit_id


class XNATSourceInputSpec(ArchiveSourceInputSpec):
    server = traits.Str(mandatory=True,
                        desc="The address of the XNAT server")
    user = traits.Str(
        mandatory=False,
        desc=("The XNAT username to connect with in with if not "
              "supplied it can be read from ~/.netrc (see "
              "https://xnat.readthedocs.io/en/latest/static/tutorial.html"
              "#connecting-to-a-server)"))
    password = traits.Password(
        mandatory=False,
        desc=("The XNAT password corresponding to the supplied username, if "
              "not supplied it can be read from ~/.netrc (see "
              "https://xnat.readthedocs.io/en/latest/static/tutorial.html"
              "#connecting-to-a-server)"))
    cache_dir = Directory(
        exists=True, desc=("Path to the base directory where the downloaded"
                           "datasets will be cached"))

    race_cond_delay = traits.Int(
        usedefault=True, default=30,
        desc=("The amount of time to wait before checking that the required "
              "dataset has been downloaded to cache by another process has "
              "completed if they are attempting to download the same dataset"))

    stagger = traits.Int(
        mandatory=False,
        desc=("Stagger the download of the required datasets by "
              "stagger_delay * subject_id seconds to avoid sending too many "
              "concurrent requests to XNAT"))


class XNATSource(ArchiveSource, XNATMixin):
    """
    A NiPype IO interface for grabbing datasets off DaRIS (analogous to
    DataGrabber)
    """

    input_spec = XNATSourceInputSpec

    def __init__(self, *args, **kwargs):
        self._check_md5 = kwargs.pop('check_md5', True)
        super(XNATSource, self).__init__(*args, **kwargs)

    @property
    def check_md5(self):
        return self._check_md5

    def _list_outputs(self):
        # FIXME: Should probably not prepend the project before this point
        subject_id = self.inputs.subject_id.split('_')[-1]
        visit_id = self.inputs.visit_id
        base_cache_dir = os.path.join(self.inputs.cache_dir,
                                      self.inputs.project_id)
        sess_kwargs = {}
        if isdefined(self.inputs.user):
            sess_kwargs['user'] = self.inputs.user
        if isdefined(self.inputs.password):
            sess_kwargs['password'] = self.inputs.password
        with xnat.connect(server=self.inputs.server,
                          **sess_kwargs) as xnat_login:
            project = xnat_login.projects[self.inputs.project_id]
            # Get primary session, processed and summary sessions and cache
            # dirs
            sessions = {}
            cache_dirs = {}
            for mult, processed in ([('per_session', False)] +
                                    zip(MULTIPLICITIES, repeat(True))):
                subj_label, sess_label = XNATArchive.get_labels(
                    mult, self.inputs.project_id, subject_id, visit_id)
                if mult == 'per_session' and processed:
                    sess_label += XNATArchive.PROCESSED_SUFFIX
                cache_dirs[(mult, processed)] = os.path.join(
                    base_cache_dir, subj_label, sess_label)
                try:
                    subject = project.subjects[subj_label]
                    sessions[(mult, processed)] = subject.experiments[
                        sess_label]
                except KeyError:
                    continue
            outputs = {}
            for (name, data_format_name, mult,
                 processed, is_spec) in self.inputs.datasets:
                # Prepend study name if defined and processed input
                prefixed_name = self.prefix_study_name(name, is_spec)
                data_format = data_formats[data_format_name]
                session = sessions[(mult, processed)]
                cache_dir = cache_dirs[(mult, processed)]
                try:
                    dataset = session.scans[prefixed_name]
                except KeyError:
                    raise NiAnalysisError(
                        "Could not find '{}' dataset in session '{}' "
                        "(found {})".format(
                            prefixed_name, session.label,
                            "', '".join(session.scans.keys())))
                # Get filename
                fname = prefixed_name
                if data_format.extension is not None:
                    fname += data_format.extension
                # Get resource to check its MD5 digest
                try:
                    resource = dataset.resources[
                        data_format.xnat_resource_name]
                except KeyError:
                    raise NiAnalysisError(
                        "'{}' dataset is not available in '{}' format, "
                        "available resources are '{}'"
                        .format(
                            name, data_format.xnat_resource_name,
                            "', '".join(
                                r.label
                                for r in dataset.resources.values())))
                need_to_download = True
                # FIXME: Should do a check to see if versions match
                if not os.path.exists(cache_dir):
                    os.makedirs(cache_dir)
                cache_path = os.path.join(cache_dir, fname)
                if os.path.exists(cache_path):
                    if self.check_md5:
                        try:
                            with open(cache_path +
                                      XNATArchive.MD5_SUFFIX) as f:
                                cached_digests = json.load(f)
                            digests = self._get_digests(resource)
                            if cached_digests == digests:
                                need_to_download = False
                        except IOError:
                            pass
                    else:
                        need_to_download = False
                if need_to_download:
                    # The path to the directory which the files will be
                    # downloaded to.
                    tmp_dir = cache_path + '.download'
                    try:
                        # Attempt to make tmp download directory. This will
                        # fail if another process (or previous attempt) has
                        # already created it. In that case this process will
                        # wait to see if that download finishes successfully,
                        # and if so use the cached version.
                        os.mkdir(tmp_dir)
                    except OSError as e:
                        if e.errno == errno.EEXIST:
                            # Another process may be concurrently downloading
                            # the same file to the cache. Wait for
                            # 'race_cond_delay' seconds and then check that it
                            # has been completed or assume interrupted and
                            # redownload.
                            self._delayed_download(
                                tmp_dir, resource, dataset, data_format,
                                session.label, cache_path,
                                delay=self.inputs.race_cond_delay)
                        else:
                            raise
                    else:
                        self._download_dataset(
                            tmp_dir, resource, dataset, data_format,
                            session.label, cache_path)
                outputs[name + PATH_SUFFIX] = cache_path
            for (name, dtype, mult,
                 processed, is_spec) in self.inputs.fields:
                prefixed_name = self.prefix_study_name(name, is_spec)
                session = sessions[(mult, processed)]
                outputs[name + FIELD_SUFFIX] = dtype(
                    session.fields[prefixed_name])
        return outputs

    def _get_digests(self, resource):
        """
        Downloads the MD5 digests associated with the files in a resource.
        These are saved with the downloaded files in the cache and used to
        check if the files have been updated on the server
        """
        result = resource.xnat_session.get(resource.uri + '/files')
        if result.status_code != 200:
            raise NiAnalysisError(
                "Could not download metadata for resource {}"
                .format(resource.id))
        return dict((r['Name'], r['digest'])
                    for r in result.json()['ResultSet']['Result'])

    def _download_dataset(self, tmp_dir, resource, dataset, data_format,
                          session_label, cache_path):
        # Download resource to zip file
        zip_path = os.path.join(tmp_dir, 'download.zip')
        with open(zip_path, 'w') as f:
            resource.xnat_session.download_stream(
                resource.uri + '/files', f, format='zip', verbose=True)
        digests = self._get_digests(resource)
        # Extract downloaded zip file
        expanded_dir = os.path.join(tmp_dir, 'expanded')
        with ZipFile(zip_path) as zip_file:
            zip_file.extractall(expanded_dir)
        data_path = os.path.join(
            expanded_dir, session_label, 'scans',
            (dataset.id + '-' + special_char_re.sub('_', dataset.type)),
            'resources', data_format.xnat_resource_name, 'files')
        if not data_format.directory:
            # If the dataformat is not a directory (e.g. DICOM),
            # attempt to locate a single file within the resource
            # directory with the appropriate filename and add that
            # to be the complete data path.
            fnames = os.listdir(data_path)
            match_fnames = [
                f for f in fnames
                if (split_extension(f)[-1].lower() ==
                    data_format.extension)]
            if len(match_fnames) == 1:
                data_path = os.path.join(data_path, match_fnames[0])
            else:
                raise NiAnalysisXnatArchiveMissingDatasetException(
                    "Did not find single file with extension '{}' "
                    "(found '{}') in resource '{}'"
                    .format(data_format.extension,
                            "', '".join(fnames), data_path))
        shutil.move(data_path, cache_path)
        with open(cache_path + XNATArchive.MD5_SUFFIX, 'w') as f:
            json.dump(digests, f)
        shutil.rmtree(tmp_dir)

    def _delayed_download(self, tmp_dir, resource, dataset, data_format,
                          session_label, cache_path, delay):
        logger.info("Waiting {} seconds for incomplete download of '{}' "
                    "initiated another process to finish"
                    .format(delay, cache_path))
        initial_mod_time = dir_modtime(tmp_dir)
        time.sleep(delay)
        if os.path.exists(cache_path):
            logger.info("The download of '{}' has completed "
                        "successfully in the other process, continuing"
                        .format(cache_path))
            return
        elif initial_mod_time != dir_modtime(tmp_dir):
            logger.info(
                "The download of '{}' hasn't completed yet, but it has"
                " been updated.  Waiting another {} seconds before "
                "checking again.".format(cache_path, delay))
            self._delayed_download(tmp_dir, resource, dataset,
                                   data_format, session_label,
                                   cache_path, delay)
        else:
            logger.warning(
                "The download of '{}' hasn't updated in {} "
                "seconds, assuming that it was interrupted and "
                "restarting download".format(cache_path, delay))
            shutil.rmtree(tmp_dir)
            os.mkdir(tmp_dir)
            self._download_dataset(
                tmp_dir, resource, dataset, data_format, session_label,
                cache_path)


class XNATSinkInputSpecMixin(object):
    server = traits.Str('https://mf-erc.its.monash.edu.au', mandatory=True,
                        usedefault=True, desc="The address of the MF server")
    user = traits.Str(
        mandatory=False,
        desc=("The XNAT username to connect with in with if not "
              "supplied it can be read from ~/.netrc (see "
              "https://xnat.readthedocs.io/en/latest/static/tutorial.html"
              "#connecting-to-a-server)"))
    password = traits.Password(
        mandatory=False,
        desc=("The XNAT password corresponding to the supplied username, if "
              "not supplied it can be read from ~/.netrc (see "
              "https://xnat.readthedocs.io/en/latest/static/tutorial.html"
              "#connecting-to-a-server)"))
    cache_dir = Directory(
        exists=True, desc=("Path to the base directory where the downloaded"
                           "datasets will be cached"))


class XNATSinkInputSpec(ArchiveSinkInputSpec, XNATSinkInputSpecMixin):
    pass


class XNATSubjectSinkInputSpec(ArchiveSubjectSinkInputSpec,
                               XNATSinkInputSpecMixin):
    pass


class XNATVisitSinkInputSpec(ArchiveVisitSinkInputSpec,
                                 XNATSinkInputSpecMixin):
    pass


class XNATProjectSinkInputSpec(ArchiveProjectSinkInputSpec,
                               XNATSinkInputSpecMixin):
    pass


class XNATSinkMixin(XNATMixin):
    """
    A NiPype IO interface for putting processed datasets onto DaRIS (analogous
    to DataSink)
    """

    __metaclass__ = ABCMeta

    def _list_outputs(self):
        """Execute this module.
        """
        # Initiate output
        outputs = self._base_outputs()
        out_files = []
        missing_files = []
        # Open XNAT session
        sess_kwargs = {}
        if 'user' in self.inputs.trait_names():  # Because InputSpec is dynamic
            sess_kwargs['user'] = self.inputs.user
        if 'password' in self.inputs.trait_names():
            sess_kwargs['password'] = self.inputs.password
        logger.debug("Session kwargs: {}".format(sess_kwargs))
        with xnat.connect(server=self.inputs.server,
                          **sess_kwargs) as xnat_login:
            # Add session for processed scans if not present
            session, cache_dir = self._get_session(xnat_login)
            # Make session cache dir
            if not os.path.exists(cache_dir):
                os.makedirs(cache_dir, stat.S_IRWXU | stat.S_IRWXG)
            # Loop through datasets connected to the sink and copy them to the
            # cache directory and upload to daris.
            for (name, format_name, mult,
                 processed, _) in self.inputs.datasets:
                assert mult == self.multiplicity
                assert processed, ("{} (format: {}, mult: {}) isn't processed"
                                   .format(name, format_name, mult))
                filename = getattr(self.inputs, name + PATH_SUFFIX)
                if not isdefined(filename):
                    missing_files.append(name)
                    continue  # skip the upload for this file
                dataset_format = data_formats[format_name]
                ext = dataset_format.extension
                assert split_extension(filename)[1] == ext, (
                    "Mismatching extension '{}' for format '{}' ('{}')"
                    .format(split_extension(filename)[1],
                            data_formats[format_name].name,
                            dataset_format.extension))
                src_path = os.path.abspath(filename)
                prefixed_name = self.prefix_study_name(name)
                out_fname = prefixed_name + (ext if ext is not None else '')
                # Copy to local cache
                dst_path = os.path.join(cache_dir, out_fname)
                out_files.append(dst_path)
                shutil.copyfile(src_path, dst_path)
                # Create md5 digest
                with open(dst_path) as f:
                    digests = {out_fname: hashlib.md5(f.read()).hexdigest()}
                with open(dst_path + XNATArchive.MD5_SUFFIX, 'w') as f:
                    json.dump(digests, f)
                # Upload to XNAT
                dataset = xnat_login.classes.MrScanData(
                    type=prefixed_name, parent=session)
                # Delete existing resource
                # TODO: probably should have check to see if we want to
                #       override it
                try:
                    resource = dataset.resources[format_name.upper()]
                    resource.delete()
                except KeyError:
                    pass
                resource = dataset.create_resource(format_name.upper())
                resource.upload(dst_path, out_fname)
            for (name, _, mult, processed, _) in self.inputs.fields:
                assert mult == self.multiplicity
                prefixed_name = self.prefix_study_name(name)
                assert processed, ("{} isn't processed".format(
                    name, format_name, mult))
                session.fields[prefixed_name] = getattr(
                    self.inputs, name + FIELD_SUFFIX)
        if missing_files:
            # FIXME: Not sure if this should be an exception or not,
            #        indicates a problem but stopping now would throw
            #        away the datasets that were created
            logger.warning(
                "Missing output datasets '{}' in XNATSink".format(
                    "', '".join(str(f) for f in missing_files)))
        # Return cache file paths
        outputs['out_files'] = out_files
        return outputs

    def _get_session(self, xnat_login):
        project = xnat_login.projects[self.inputs.project_id]
        # FIXME: Subject should probably be input without the project prefix
        try:
            subject_id = self.inputs.subject_id.split('_')[-1]
        except AttributeError:
            subject_id = None
        try:
            visit_id = self.inputs.visit_id
        except AttributeError:
            visit_id = None
        subj_label, sess_label = XNATArchive.get_labels(
            self.multiplicity, self.inputs.project_id, subject_id, visit_id)
        if self.multiplicity == 'per_session':
            sess_label += XNATArchive.PROCESSED_SUFFIX
            if visit_id is not None:
                visit_id += XNATArchive.PROCESSED_SUFFIX
        try:
            subject = project.subjects[subj_label]
        except KeyError:
            subject = xnat_login.classes.SubjectData(
                label=subj_label, parent=project)
        try:
            session = subject.experiments[sess_label]
        except KeyError:
            session = self._create_session(xnat_login, subj_label,
                                           sess_label)
        # Get cache dir for session
        cache_dir = os.path.abspath(os.path.join(
            self.inputs.cache_dir, self.inputs.project_id, subject.label,
            session.label))
        return session, cache_dir

    def _create_session(self, xnat_login, subject_id, visit_id):
        """
        This creates a processed session in a way that respects whether
        the acquired session has been shared into another project or not.

        If we weren't worried about this we could just use

            session = xnat_login.classes.MrSessionData(label=proc_session_id,
                                                       parent=subject)
        """
        uri = ('/data/archive/projects/{}/subjects/{}/experiments/{}'
               .format(self.inputs.project_id, subject_id, visit_id))
        query = {'xsiType': 'xnat:mrSessionData', 'label': visit_id,
                 'req_format': 'qa'}
        response = xnat_login.put(uri, query=query)
        if response.status_code not in (200, 201):
            raise NiAnalysisError(
                "Could not create session '{}' in subject '{}' in project '{}'"
                " response code {}"
                .format(visit_id, subject_id, self.inputs.project_id,
                        response))
        return xnat_login.classes.MrSessionData(uri=uri,
                                                xnat_session=xnat_login)


class XNATSink(XNATSinkMixin, ArchiveSink):

    input_spec = XNATSinkInputSpec

#     def _get_session(self, xnat_login):
#         project = xnat_login.projects[self.inputs.project_id]
#         subject = project.subjects[self.inputs.subject_id]
#         assert self.session_id in subject.experiments
#         session_name = self.session_id + XNATArchive.PROCESSED_SUFFIX
#         try:
#             session = subject.experiments[session_name]
#         except KeyError:
#             session = self._create_session(xnat_login, subject.id,
#                                            session_name)
#         # Get cache dir for session
#         cache_dir = os.path.abspath(os.path.join(
#             self.inputs.cache_dir, self.inputs.project_id,
#             self.inputs.subject_id,
#             self.inputs.visit_id + XNATArchive.PROCESSED_SUFFIX))
#         return session, cache_dir


class XNATSubjectSink(XNATSinkMixin, ArchiveSubjectSink):

    input_spec = XNATSubjectSinkInputSpec
# 
#     def _get_session(self, xnat_login):
#         project = xnat_login.projects[self.inputs.project_id]
#         subject = project.subjects[self.inputs.subject_id]
#         subject_name, session_name = XNATArchive.get_labels(
#             self.multiplicity, *self.inputs.subject_id.split('_'))
#         try:
#             session = subject.experiments[session_name]
#         except KeyError:
#             session = self._create_session(xnat_login, subject.id,
#                                            session_name)
#         # Get cache dir for session
#         cache_dir = os.path.abspath(os.path.join(
#             self.inputs.cache_dir, self.inputs.project_id,
#             subject_name, session_name))
#         return session, cache_dir


class XNATVisitSink(XNATSinkMixin, ArchiveVisitSink):

    input_spec = XNATVisitSinkInputSpec
# 
#     def _get_session(self, xnat_login):
#         project = xnat_login.projects[self.inputs.project_id]
#         subject_name, session_name = XNATArchive.get_labels(
#             self.multiplicity, self.inputs.project_id, self.inputs.visit_id)
#         try:
#             subject = project.subjects[subject_name]
#         except KeyError:
#             subject = xnat_login.classes.SubjectData(
#                 label=subject_name, parent=project)
#         try:
#             session = subject.experiments[session_name]
#         except KeyError:
#             session = self._create_session(xnat_login, subject.id,
#                                            session_name)
#         # Get cache dir for session
#         cache_dir = os.path.abspath(os.path.join(
#             self.inputs.cache_dir, self.inputs.project_id,
#             subject_name, session_name))
#         return session, cache_dir


class XNATProjectSink(XNATSinkMixin, ArchiveProjectSink):

    input_spec = XNATProjectSinkInputSpec
# 
#     def _get_session(self, xnat_login):
#         project = xnat_login.projects[self.inputs.project_id]
#         subject_name, session_name = XNATArchive.project_summary_name(
#             self.inputs.project_id)
#         try:
#             subject = project.subjects[subject_name]
#         except KeyError:
#             subject = xnat_login.classes.SubjectData(
#                 label=subject_name, parent=project)
#         try:
#             session = subject.experiments[session_name]
#         except KeyError:
#             session = xnat_login.classes.MrSessionData(
#                 label=session_name, parent=subject)
#         # Get cache dir for session
#         cache_dir = os.path.abspath(os.path.join(
#             self.inputs.cache_dir, self.inputs.project_id,
#             subject_name, session_name))
#         return session, cache_dir


class XNATArchive(Archive):
    """
    An 'Archive' class for the DaRIS research management system.

    Parameters
    ----------
    user : str
        Username with which to connect to XNAT with
    password : str
        Password to connect to XNAt with
    cache_dir : str (path)
        Path to local directory to cache XNAT data in
    server : str (URI)
        URI of XNAT server to connect to
    check_md5 : bool
        Whether to check the MD5 digest of cached files before using. This
        checks for updates on the server since the file was cached
    """

    type = 'xnat'
    Sink = XNATSink
    Source = XNATSource
    SubjectSink = XNATSubjectSink
    VisitSink = XNATVisitSink
    ProjectSink = XNATProjectSink

    SUMMARY_NAME = 'ALL'
    PROCESSED_SUFFIX = '_PROC'
    MD5_SUFFIX = '.md5.json'

    def __init__(self, user=None, password=None, cache_dir=None,
                 server='https://mbi-xnat.erc.monash.edu.au',
                 check_md5=True):
        self._server = server
        self._user = user
        self._password = password
        if cache_dir is None:
            self._cache_dir = os.path.join(os.environ['HOME'], '.xnat')
        else:
            self._cache_dir = cache_dir
        try:
            # Attempt to make cache if it doesn't already exist
            os.makedirs(self._cache_dir)
        except OSError as e:
            if e.errno != errno.EEXIST:
                raise
        self._check_md5 = check_md5

    def source(self, *args, **kwargs):
        source = super(XNATArchive, self).source(*args, **kwargs)
        source.inputs.server = self._server
        if self._user is not None:
            source.inputs.user = self._user
        if self._password is not None:
            source.inputs.password = self._password
        source.inputs.cache_dir = self._cache_dir
        return source

    def sink(self, *args, **kwargs):
        sink = super(XNATArchive, self).sink(*args, **kwargs)
        sink.inputs.server = self._server
        if self._user is not None:
            sink.inputs.user = self._user
        if self._password is not None:
            sink.inputs.password = self._password
        sink.inputs.cache_dir = self._cache_dir
        return sink

    def _login(self):
        sess_kwargs = {}
        if self._user is not None:
            sess_kwargs['user'] = self._user
        if self._password is not None:
            sess_kwargs['password'] = self._password
        return xnat.connect(server=self._server, **sess_kwargs)

    def cache(self, project_id, datasets, subject_ids=None,
              visit_ids=None, work_dir=None):
        """
        Creates the local cache of datasets. Useful when launching many
        parallel jobs that will all try to pull the data through tomcat
        server at the same time, and probably lead to timeout errors.

        Parameters
        ----------
        datasets : list(Dataset)
            List of datasets to download to the cache
        subject_ids : list(str | int) | None
            List of subjects to download the datasets for. If None the datasets
            will be downloaded for all subjects
        filter_session_ids : list(str) | None
            List of sessions to download the datasets for. If None all sessions
            will be downloaded.
        """
        workflow = pe.Workflow(name='cache_download', base_dir=work_dir)
        subjects = pe.Node(InputSubjects(), name='subjects')
        sessions = pe.Node(InputSessions(), name='sessions')
        subjects.iterables = ('subject_id', tuple(subject_ids))
        sessions.iterables = ('visit_id', tuple(visit_ids))
        source = self.source(project_id, datasets, study_name='cache')
        workflow.connect(subjects, 'subject_id', sessions, 'subject_id')
        workflow.connect(sessions, 'subject_id', source, 'subject_id')
        workflow.connect(sessions, 'visit_id', source, 'visit_id')
        workflow.run()

    def all_session_ids(self, project_id):
        """
        Parameters
        ----------
        project_id : int
            The project id to return the sessions for
        repo_id : int
            The id of the repository (2 for monash daris)
        visit_ids: int|List[int]|None
            Id or ids of sessions of which to return sessions for. If None all
            are returned
        """
        sess_kwargs = {}
        if self._user is not None:
            sess_kwargs['user'] = self._user
        if self._password is not None:
            sess_kwargs['password'] = self._password
        with self.login() as xnat_login:
            return [
                s.label for s in xnat_login.projects[
                    project_id].experiments.itervalues()]

    def project(self, project_id, subject_ids=None, visit_ids=None):
        """
        Return subject and session information for a project in the XNAT
        archive

        Parameters
        ----------
        project_id : str
            ID of the project to inspect
        subject_ids : list(str)
            List of subject IDs with which to filter the tree with. If None all
            are returned
        visit_ids : list(str)
            List of visit IDs with which to filter the tree with. If None all
            are returned

        Returns
        -------
        project : nianalysis.archive.Project
            A hierarchical tree of subject, session and dataset information for
            the archive
        """
        # Convert subject ids to strings if they are integers
        if subject_ids is not None:
            subject_ids = [('{}_{:03d}'.format(project_id, s)
                            if isinstance(s, int) else s) for s in subject_ids]
        # Add processed visit IDs to list of visit ids to filter
        if visit_ids is not None:
            visit_ids = visit_ids + [i + self.PROCESSED_SUFFIX
                                     for i in visit_ids]
        subjects = []
        sessions = defaultdict(list)
        with self._login() as xnat_login:
            xproject = xnat_login.projects[project_id]
            visit_sessions = defaultdict(list)
            # Create list of subjects
            for xsubject in xproject.subjects.itervalues():
                subj_id = xsubject.label
                logger.debug("Getting info for subject '{}'".format(subj_id))
                if not (subject_ids is None or subj_id in subject_ids):
                    continue
                sessions = {}
                proc_sessions = []
                # Get per_session datasets
                for xsession in xsubject.experiments.itervalues():
                    visit_id = '_'.join(xsession.label.split('_')[2:])
                    if not (visit_ids is None or visit_id in visit_ids):
                        continue
                    processed = xsession.label.endswith(
                        self.PROCESSED_SUFFIX)
                    session = Session(subj_id, visit_id,
                                      datasets=self._get_datasets(
                                          xsession, 'per_session',
                                          processed=processed),
                                      fields=self._get_fields(
                                          xsession, 'per_session',
                                          processed=processed),
                                      processed=None)
                    if processed:
                        proc_sessions.append(session)
                    else:
                        sessions[visit_id] = session
                        visit_sessions[visit_id].append(session)
                for proc_session in proc_sessions:
                    visit_id = proc_session.visit_id[:-len(
                        self.PROCESSED_SUFFIX)]
                    try:
                        sessions[visit_id].processed = proc_session
                    except KeyError:
                        raise NiAnalysisError(
                            "No matching acquired session for processed "
                            "session '{}_{}_{}'".format(
                                project_id,
                                proc_session.subject_id,
                                proc_session.visit_id))
                # Get per_subject datasets
                _, subj_summary_name = self.get_labels(
                    'per_subject', *subj_id.split('_'))
                try:
                    xsubj_summary = xsubject.experiments[subj_summary_name]
                except KeyError:
                    subj_datasets = []
                    subj_fields = []
                else:
                    subj_datasets = self._get_datasets(
                        xsubj_summary, 'per_subject', processed=True)
                    subj_fields = self._get_fields(
                        xsubj_summary, 'per_subject', processed=True)
                subjects.append(Subject(subj_id, sessions.values(),
                                        datasets=subj_datasets,
                                        fields=subj_fields))
            # Create list of visits
            visits = []
            for visit_id, sessions in visit_sessions.iteritems():
                (_, visit_summary_sess_name) = self.get_labels(
                    'per_visit', project_id, visit_id=visit_id)
                # Get 'per_visit' datasets
                try:
                    xvisit_summary = xproject.experiments[
                        visit_summary_sess_name]
                except KeyError:
                    visit_datasets = []
                    visit_fields = {}
                else:
                    visit_datasets = self._get_datasets(xvisit_summary,
                                                        'per_visit',
                                                        processed=True)
                    visit_fields = self._get_fields(xvisit_summary,
                                                    'per_visit',
                                                    processed=True)
                visits.append(Visit(visit_id, sessions,
                                    datasets=visit_datasets,
                                    fields=visit_fields))
            # Get 'per_project' datasets
            (proj_summary_subj_name,
             proj_summary_sess_name) = self.get_labels('per_project',
                                                       project_id)
            try:
                xproj_summary = xproject.subjects[
                    proj_summary_subj_name].experiments[proj_summary_sess_name]
            except KeyError:
                proj_datasets = []
                proj_fields = []
            else:
                proj_datasets = self._get_datasets(xproj_summary,
                                                   'per_project',
                                                   processed=True)
                proj_fields = self._get_fields(xproj_summary, 'per_project',
                                               processed=True)
            if not subjects:
                raise NiAnalysisError(
                    "Did not find any subjects matching the IDs '{}' in "
                    "project '{}' (found '{}')"
                    .format("', '".join(subject_ids), project_id,
                            "', '".join(s.label for s in xproject.subjects)))
            if not sessions:
                raise NiAnalysisError(
                    "Did not find any sessions subjects matching the IDs '{}'"
                    "(in subjects '{}') for project '{}'"
                    .format("', '".join(visit_ids),
                            "', '".join(s.label for s in xproject.subjects),
                             project_id))
        return Project(project_id, subjects, visits, datasets=proj_datasets,
                       fields=proj_fields)

    def _get_datasets(self, xsession, mult, processed):
        """
        Returns a list of datasets within an XNAT session

        Parameters
        ----------
        xsession : xnat.classes.MrSessionData
            The XNAT session to extract the datasets from
        mult : str
            The multiplicity of the returned datasets (either 'per_session',
            'per_subject', 'per_visit', or 'per_project')

        Returns
        -------
        datasets : list(nianalysis.dataset.Dataset)
            List of datasets within an XNAT session
        """
        datasets = []
        for dataset in xsession.scans.itervalues():
            datasets.append(Dataset(
                dataset.type, format=None, processed=processed,  # @ReservedAssignment @IgnorePep8
                multiplicity=mult, location=None))
        return datasets

    def _get_fields(self, xsession, mult, processed):
        """
        Returns a list of fields within an XNAT session

        Parameters
        ----------
        xsession : xnat.classes.MrSessionData
            The XNAT session to extract the fields from
        mult : str
            The multiplicity of the returned fields (either 'per_session',
            'per_subject', 'per_visit', or 'per_project')

        Returns
        -------
        fields : list(nianalysis.dataset.Dataset)
            List of fields within an XNAT session
        """
        fields = []
        for name, value in xsession.fields.items():
            # Try convert to each datatypes in order of specificity to
            # determine type
            for dtype in (int, float, str):
                try:
                    dtype(value)
                    break
                except ValueError:
                    continue
            fields.append(Field(
                name=name, dtype=dtype, processed=processed,  # @ReservedAssignment @IgnorePep8
                multiplicity=mult))
        return fields

    @property
    def local_dir(self):
        return self._cache_dir

    @classmethod
    def get_labels(cls, multiplicity, project_id, subject_id=None,
                   visit_id=None):
        if multiplicity == 'per_session':
            subj_label = '{}_{}'.format(project_id, subject_id)
            sess_label = '{}_{}_{}'.format(project_id, subject_id,
                                           visit_id)
        elif multiplicity == 'per_subject':
            subj_label = '{}_{}'.format(project_id, subject_id)
            sess_label = '{}_{}_{}'.format(project_id, subject_id,
                                           cls.SUMMARY_NAME)
        elif multiplicity == 'per_visit':
            subj_label = '{}_{}'.format(project_id, cls.SUMMARY_NAME)
            sess_label = '{}_{}_{}'.format(project_id, cls.SUMMARY_NAME,
                                           visit_id)
        elif multiplicity == 'per_project':
            subj_label = '{}_{}'.format(project_id, cls.SUMMARY_NAME)
            sess_label = '{}_{}_{}'.format(project_id, cls.SUMMARY_NAME,
                                           cls.SUMMARY_NAME)
        else:
            raise NiAnalysisError(
                "Unrecognised multiplicity '{}'".format(multiplicity))
        return (subj_label, sess_label)


def download_all_datasets(download_dir, server, session_id, overwrite=True,
                          **kwargs):
    with xnat.connect(server, **kwargs) as xnat_login:
        try:
            session = xnat_login.experiments[session_id]
        except KeyError:
            raise NiAnalysisError(
                "Didn't find session matching '{}' on {}".format(session_id,
                                                                 server))
        for dataset in session.scans.itervalues():
            data_format_name = _guess_data_format(dataset)
            ext = data_formats[data_format_name.lower()].extension
            if ext is None:
                ext = ''
            download_path = os.path.join(download_dir, dataset.type + ext)
            if overwrite or not os.path.exists(download_path):
                download_resource(download_path, dataset,
                                  data_format_name, session.label)


def download_dataset(download_path, server, user, password, session_id,
                     dataset_name, data_format=None):
    """
    Downloads a single dataset from an XNAT server
    """
    with xnat.connect(server, user=user, password=password) as xnat_login:
        try:
            session = xnat_login.experiments[session_id]
        except KeyError:
            raise NiAnalysisError(
                "Didn't find session matching '{}' on {}".format(session_id,
                                                                 server))
        try:
            dataset = session.scans[dataset_name]
        except KeyError:
            raise NiAnalysisError(
                "Didn't find dataset matching '{}' in {}".format(dataset_name,
                                                                 session_id))
        if data_format is None:
            data_format = _guess_data_format(dataset)
        download_resource(download_path, dataset, data_format, session.label)


def _guess_data_format(dataset):
    dataset_formats = [r for r in dataset.resources.itervalues()
                       if r.label.lower() in data_formats]
    if len(dataset_formats) > 1:
        raise NiAnalysisError(
            "Multiple valid resources '{}' for '{}' dataset, please pass "
            "'data_format' to 'download_dataset' method to speficy resource to"
            "download".format("', '".join(dataset_formats), dataset.type))
    elif not dataset_formats:
        raise NiAnalysisError(
            "No recognised data formats for '{}' dataset (available resources "
            "are '{}')".format(
                dataset.type, "', '".join(
                    r.label for r in dataset.resources.itervalues())))
    return dataset_formats[0].label


def download_resource(download_path, dataset, data_format_name,
                      session_label):

    data_format = data_formats[data_format_name.lower()]
    try:
        resource = dataset.resources[data_format.xnat_resource_name]
    except KeyError:
        raise NiAnalysisError(
            "Didn't find {} resource in {} dataset matching '{}' in {}"
            .format(data_format.xnat_resource_name, dataset.type))
    tmp_dir = download_path + '.download'
    resource.download_dir(tmp_dir)
    dataset_label = dataset.id + '-' + special_char_re.sub('_', dataset.type)
    src_path = os.path.join(tmp_dir, session_label, 'scans',
                            dataset_label, 'resources',
                            data_format.xnat_resource_name, 'files')
    if not data_format.directory:
        fnames = os.listdir(src_path)
        match_fnames = [
            f for f in fnames
            if split_extension(f)[-1].lower() == data_format.extension]
        if len(match_fnames) == 1:
            src_path = os.path.join(src_path, match_fnames[0])
        else:
            raise NiAnalysisXnatArchiveMissingDatasetException(
                "Did not find single file with extension '{}' "
                "(found '{}') in resource '{}'"
                .format(data_format.extension,
                        "', '".join(fnames), src_path))
    shutil.move(src_path, download_path)
    shutil.rmtree(tmp_dir)


def list_datasets(server, user, password, session_id):
    with xnat.connect(server, user=user, password=password) as xnat_login:
        session = xnat_login.experiments[session_id]
        return [s.type for s in session.scans.itervalues()]
