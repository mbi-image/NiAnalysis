import os
import shutil
from arcana import (
    StudyMetaClass, Study, LocalArchive, LinearRunner, DatasetSpec,
    DatasetMatch)
from arcana.data_format import text_format
import cPickle as pkl
import os.path as op
from nipype.interfaces.utility import IdentityInterface

DATA_DIR = op.join(op.dirname(__file__), 'data', 'gen_cls')
ARCHIVE_DIR = op.join(DATA_DIR, 'archive')
WORK_DIR = op.join(DATA_DIR, 'work')
GEN_PKL_PATH = op.join(DATA_DIR, 'gen.pkl')
NORM_PKL_PATH = op.join(DATA_DIR, 'normal.pkl')

shutil.rmtree(DATA_DIR, ignore_errors=True)
os.makedirs(WORK_DIR)

SESS_DIR = op.join(ARCHIVE_DIR, 'SUBJECT', 'SESSION')
os.makedirs(SESS_DIR)
with open(op.join(SESS_DIR, 'dataset.txt'), 'w') as f:
    f.write('blah blah')

#     name : str
#         The name of the pipeline
#     study : Study
#         The study from which the pipeline was created
#     inputs : List[DatasetSpec|FieldSpec]
#         The list of input datasets required for the pipeline
#         un/processed datasets, and the options used to generate them for
#         unprocessed datasets
#     outputs : List[DatasetSpec|FieldSpec]
#         The list of outputs (hard-coded names for un/processed datasets)
#     citations : List[Citation]
#         List of citations that describe the workflow and should be cited in
#         publications
#     version : int
#         A version number for the pipeline to be incremented whenever the output
#         of the pipeline
#     name_prefix : str
#         Prefix prepended to the name of the pipeline. Typically passed
#         in from a kwarg of the pipeline constructor method to allow
#         multi-classes to alter the name of the pipeline to avoid name
#         clashes
#     add_inputs : List[DatasetSpec|FieldSpec]
#         Additional inputs to append to the inputs argument. Typically
#         passed in from a kwarg of the pipeline constructor method to
#         allow sub-classes to add additional inputs
#     add_outputs : List[DatasetSpec|FieldSpec]
#         Additional outputs to append to the outputs argument. Typically
#         passed in from a kwarg of the pipeline constructor method to
#         allow sub-classes to add additional outputs


class NormalClass(Study):

    __metaclass__ = StudyMetaClass

    add_data_specs = [DatasetSpec('dataset', text_format),
                      DatasetSpec('out_dataset', text_format,
                                  'pipeline')]

    def pipeline(self):
        pipeline = self.create_pipeline(
            'pipeline',
            inputs=[DatasetSpec('dataset', text_format)],
            outputs=[DatasetSpec('out_dataset', text_format)],
            desc='a dummy pipeline',
            citations=[],
            version=1)
        ident = pipeline.create_node(IdentityInterface(['dataset']),
                                     name='ident')
        pipeline.connect_input('dataset', ident, 'dataset')
        pipeline.connect_output('out_dataset', ident, 'dataset')
        return pipeline


GeneratedClass = StudyMetaClass(
    'GeneratedClass', (NormalClass,), {})


norm = NormalClass('norm', LocalArchive(ARCHIVE_DIR),
                   LinearRunner(WORK_DIR),
                   inputs=[DatasetMatch('dataset', text_format,
                                           'dataset')])


gen = GeneratedClass('gen', LocalArchive(ARCHIVE_DIR),
                     LinearRunner(WORK_DIR),
                     inputs=[DatasetMatch('dataset', text_format,
                                          'dataset')])

print(norm)
print(gen)


with open(NORM_PKL_PATH, 'w') as f:
    pkl.dump(norm, f)

with open(GEN_PKL_PATH, 'w') as f:
    pkl.dump(gen, f)

del gen, GeneratedClass

with open(NORM_PKL_PATH) as f:
    renorm = pkl.load(f)

with open(GEN_PKL_PATH) as f:
    regen = pkl.load(f)

regen.data('out_dataset')

print(regen)
