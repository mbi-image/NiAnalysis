from arcana import (Study, StudyMetaClass, FilesetSpec, FieldSpec,
                    ParameterSpec, SwitchSpec)
from arcana.data.file_format.standard import text_format
from nianalysis.file_format import nifti_gz_format, dicom_format


class ExampleStudy(Study, metaclass=StudyMetaClass):

    add_data_specs = [
        FilesetSpec('acquired_file1', text_format),
        FilesetSpec('acquired_file2', dicom_format),
        FieldSpec('acquired_field1', int),
        FieldSpec('acquired_field2', str),
        FilesetSpec('derived_file1', text_format, 'pipeline1'),
        FilesetSpec('derived_file2', nifti_gz_format, 'pipeline1'),
        FilesetSpec('derived_file3', text_format, 'pipeline2'),
        FieldSpec('derived_field4', float, 'pipeline2'),
        FilesetSpec('derived_file4', dicom_format, 'pipeline3'),
        FilesetSpec('derived_file5', nifti_gz_format, 'pipeline3',
                    frequency='per_subject'),
        FilesetSpec('derived_file6', text_format, 'pipeline4',
                    frequency='per_visit'),
        FieldSpec('derived_field7', int, 'pipeline4',
                  frequency='per_study')]

    add_parameter_specs = [
        ParameterSpec('parameter1', False),
        ParameterSpec('parameter2', 25.8),
        SwitchSpec('pipeline_tool', 'toolA', ('toolA', 'toolB'))]
