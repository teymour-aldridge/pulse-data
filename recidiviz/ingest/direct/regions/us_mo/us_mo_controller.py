# Recidiviz - a data platform for criminal justice reform
# Copyright (C) 2019 Recidiviz, Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
# =============================================================================
"""Direct ingest controller implementation for us_mo."""

from typing import Optional, List, Callable, Dict

from recidiviz.common.constants.state.external_id_types import US_MO_DOC, \
    US_MO_SID, US_MO_FBI, US_MO_OLN
from recidiviz.common.ingest_metadata import SystemLevel
from recidiviz.ingest.direct.controllers.csv_gcsfs_direct_ingest_controller \
    import CsvGcsfsDirectIngestController
from recidiviz.ingest.direct.direct_ingest_controller_utils import \
    create_if_not_exists
from recidiviz.ingest.direct.state_shared_row_posthooks import \
    copy_name_to_alias, gen_label_single_external_id_hook
from recidiviz.ingest.models.ingest_info import IngestObject, StatePerson, \
    StatePersonExternalId, StateSentenceGroup
from recidiviz.ingest.models.ingest_object_cache import IngestObjectCache


class UsMoController(CsvGcsfsDirectIngestController):
    """Direct ingest controller implementation for us_mo."""

    FILE_TAGS = [
        'tak001_offender_identification',
        'tak040_offender_cycles',
    ]

    PRIMARY_COL_PREFIXES_BY_FILE_TAG = {
        'tak001_offender_identification': 'EK',
        'tak040_offender_cycles': 'DQ',
    }

    def __init__(self,
                 ingest_directory_path: Optional[str] = None,
                 storage_directory_path: Optional[str] = None,
                 max_delay_sec_between_files: Optional[int] = None):
        super(UsMoController, self).__init__(
            'us_mo',
            SystemLevel.STATE,
            ingest_directory_path,
            storage_directory_path,
            max_delay_sec_between_files=max_delay_sec_between_files)

        self.row_pre_processors_by_file: Dict[str, List[Callable]] = {}
        self.row_post_processors_by_file: Dict[str, List[Callable]] = {
            'tak001_offender_identification': [
                copy_name_to_alias,
                # When first parsed, the info object just has a single
                # external id - the DOC id.
                gen_label_single_external_id_hook(US_MO_DOC),
                self.tak001_offender_identification_hydrate_alternate_ids,
                self.normalize_sentence_group_ids,
            ],
            'tak040_offender_cycles': [
                gen_label_single_external_id_hook(US_MO_DOC),
                self.normalize_sentence_group_ids,
            ]
        }
        self.primary_key_override_by_file: Dict[str, Callable] = {}

    def _get_file_tag_rank_list(self) -> List[str]:
        return self.FILE_TAGS

    def _get_row_pre_processors_for_file(self,
                                         file_tag: str) -> List[Callable]:
        return self.row_pre_processors_by_file.get(file_tag, [])

    def _get_row_post_processors_for_file(self,
                                          file_tag: str) -> List[Callable]:
        return self.row_post_processors_by_file.get(file_tag, [])

    def _get_primary_key_override_for_file(
            self, file_tag: str) -> Optional[Callable]:
        return self.primary_key_override_by_file.get(file_tag, None)

    # TODO(1882): If yaml format supported raw values and multiple children of
    #  the same type, then this would be no-longer necessary.
    @staticmethod
    def tak001_offender_identification_hydrate_alternate_ids(
            _file_tag: str,
            _row: Dict[str, str],
            extracted_objects: List[IngestObject],
            _cache: IngestObjectCache):
        for extracted_object in extracted_objects:
            if isinstance(extracted_object, StatePerson):
                external_ids_to_create = [
                    StatePersonExternalId(
                        state_person_external_id_id=_row['EK$SID'],
                        id_type=US_MO_SID),
                    StatePersonExternalId(
                        state_person_external_id_id=_row['EK$FBI'],
                        id_type=US_MO_FBI),
                    StatePersonExternalId(
                        state_person_external_id_id=_row['EK$OLN'],
                        id_type=US_MO_OLN)
                    ]

                for id_to_create in external_ids_to_create:
                    create_if_not_exists(
                        id_to_create,
                        extracted_object,
                        'state_person_external_ids')

    @classmethod
    def normalize_sentence_group_ids(
            cls,
            file_tag: str,
            row: Dict[str, str],
            extracted_objects: List[IngestObject],
            _cache: IngestObjectCache):
        col_prefix = cls.primary_col_prefix_for_file_tag(file_tag)
        for obj in extracted_objects:
            if isinstance(obj, StateSentenceGroup):
                obj.__setattr__(
                    'state_sentence_group_id',
                    cls._generate_sentence_group_id(col_prefix, row))


    @classmethod
    def _generate_sentence_group_id(cls,
                                    col_prefix: str,
                                    row: Dict[str, str]) -> str:
        doc_id = row[f'{col_prefix}$DOC']
        cyc_id = row[f'{col_prefix}$CYC']
        return f'{doc_id}-{cyc_id}'

    @classmethod
    def primary_col_prefix_for_file_tag(cls, file_tag: str) -> str:
        return cls.PRIMARY_COL_PREFIXES_BY_FILE_TAG[file_tag]