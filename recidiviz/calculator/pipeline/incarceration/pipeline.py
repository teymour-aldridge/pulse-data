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
"""Runs the incarceration calculation pipeline. See recidiviz/tools/run_calculation_pipelines.py for details on how to
run.
"""

from __future__ import absolute_import

import argparse
import logging

from typing import Any, Dict, List, Tuple, Set, Optional
import datetime

from apache_beam.pvalue import AsDict
from more_itertools import one

import apache_beam as beam
from apache_beam.options.pipeline_options import SetupOptions, PipelineOptions
from apache_beam.typehints import with_input_types, with_output_types

from recidiviz.calculator.pipeline.incarceration import identifier, calculator
from recidiviz.calculator.pipeline.incarceration.incarceration_event import \
    IncarcerationEvent
from recidiviz.calculator.pipeline.incarceration.metrics import \
    IncarcerationMetric, IncarcerationAdmissionMetric, \
    IncarcerationReleaseMetric, IncarcerationPopulationMetric, IncarcerationMetricType
from recidiviz.calculator.pipeline.utils.beam_utils import ConvertDictToKVTuple
from recidiviz.calculator.pipeline.utils.entity_hydration_utils import SetSentencesOnSentenceGroup, \
    ConvertSentencesToStateSpecificType
from recidiviz.calculator.pipeline.utils.execution_utils import get_job_id
from recidiviz.calculator.pipeline.utils.extractor_utils import BuildRootEntity
from recidiviz.calculator.pipeline.utils.pipeline_args_utils import add_shared_pipeline_arguments
from recidiviz.persistence.database.schema.state import schema
from recidiviz.persistence.entity.state import entities
from recidiviz.utils import environment
from recidiviz.calculator.pipeline.utils.metric_utils import json_serializable_metric_key

# Cached job_id value
_job_id = None


def job_id(pipeline_options: Dict[str, str]) -> str:
    global _job_id
    if not _job_id:
        _job_id = get_job_id(pipeline_options)
    return _job_id


@environment.test_only
def clear_job_id():
    global _job_id
    _job_id = None


@with_input_types(beam.typehints.Tuple[entities.StatePerson, List[IncarcerationEvent]])
@with_output_types(IncarcerationMetric)
class GetIncarcerationMetrics(beam.PTransform):
    """Transforms a StatePerson and IncarcerationEvents into IncarcerationMetrics."""

    def __init__(self, pipeline_options: Dict[str, str],
                 metric_types: Set[str],
                 calculation_month_count: int,
                 calculation_end_month: Optional[str] = None):
        super(GetIncarcerationMetrics, self).__init__()
        self._pipeline_options = pipeline_options
        self._calculation_end_month = calculation_end_month
        self._calculation_month_count = calculation_month_count

        month_count_string = str(calculation_month_count) if calculation_month_count != -1 else 'all'
        end_month_string = calculation_end_month if calculation_end_month else 'the current month'
        logging.info("Producing metric output for %s month(s) up to %s", month_count_string, end_month_string)

        self._metric_inclusions: Dict[IncarcerationMetricType, bool] = {}

        for metric_option in IncarcerationMetricType:
            if metric_option.value in metric_types or 'ALL' in metric_types:
                self._metric_inclusions[metric_option] = True
                logging.info("Producing %s metrics", metric_option.value)
            else:
                self._metric_inclusions[metric_option] = False

    def expand(self, input_or_inputs):
        # Calculate incarceration metric combinations from a StatePerson and their IncarcerationEvents
        incarceration_metric_combinations = (
            input_or_inputs
            | 'Map to metric combinations' >>
            beam.ParDo(CalculateIncarcerationMetricCombinations(),
                       self._calculation_end_month, self._calculation_month_count, self._metric_inclusions))

        # Produce IncarcerationMetrics
        incarceration_metrics = (incarceration_metric_combinations |
                                 'Produce IncarcerationMetrics' >>
                                 beam.ParDo(ProduceIncarcerationMetric(), **self._pipeline_options))

        # Return IncarcerationMetric objects
        return incarceration_metrics


@with_input_types(beam.typehints.Tuple[int, Dict[str, Any]],
                  beam.typehints.Optional[Dict[Any, Tuple[Any, Dict[str, Any]]]])
@with_output_types(beam.typehints.Tuple[entities.StatePerson,
                                        List[IncarcerationEvent]])
class ClassifyIncarcerationEvents(beam.DoFn):
    """Classifies incarceration periods as admission and release events."""

    # pylint: disable=arguments-differ
    def process(self, element, person_id_to_county):
        """Identifies instances of admission and release from incarceration."""
        _, person_entities = element

        # Get the StateSentenceGroups as a list
        sentence_groups = list(person_entities['sentence_groups'])

        # Get the StatePerson
        person = one(person_entities['person'])

        # Get the person's county of residence, if present
        person_id_to_county_fields = person_id_to_county.get(person.person_id, None)
        county_of_residence = person_id_to_county_fields.get('county_of_residence', None) \
            if person_id_to_county_fields else None

        # Find the IncarcerationEvents
        incarceration_events = identifier.find_incarceration_events(sentence_groups, county_of_residence)

        if not incarceration_events:
            logging.info("No valid incarceration events for person with id: %d. Excluding them from the "
                         "calculations.", person.person_id)
        else:
            yield (person, incarceration_events)

    def to_runner_api_parameter(self, _):
        pass  # Passing unused abstract method.


@with_input_types(beam.typehints.Tuple[entities.StatePerson, Dict[int, List[IncarcerationEvent]]],
                  beam.typehints.Optional[str],
                  beam.typehints.Optional[int],
                  beam.typehints.Dict[IncarcerationMetricType, bool])
@with_output_types(beam.typehints.Tuple[Dict[str, Any], Any])
class CalculateIncarcerationMetricCombinations(beam.DoFn):
    """Calculates incarceration metric combinations."""

    #pylint: disable=arguments-differ
    def process(self, element, calculation_end_month, calculation_month_count, metric_inclusions):
        """Produces various incarceration metric combinations.

        Sends the calculator the StatePerson entity and their corresponding IncarcerationEvents for mapping all
        incarceration combinations.

        Args:
            element: Tuple containing a StatePerson and their IncarcerationEvents
            calculation_end_month: The year and month of the last month for which metrics should be calculated.
            calculation_month_count: The number of months to limit the monthly calculation output to.
            metric_inclusions: A dictionary where the keys are each IncarcerationMetricType, and the values are boolean
                flags for whether or not to include that metric type in the calculations
        Yields:
            Each incarceration metric combination.
        """
        person, incarceration_events = element

        # Calculate incarceration metric combinations for this person and events
        metric_combinations = calculator.map_incarceration_combinations(person,
                                                                        incarceration_events,
                                                                        metric_inclusions,
                                                                        calculation_end_month,
                                                                        calculation_month_count)

        # Return each of the incarceration metric combinations
        for metric_combination in metric_combinations:
            yield metric_combination

    def to_runner_api_parameter(self, _):
        pass  # Passing unused abstract method.


@with_input_types(beam.typehints.Tuple[Dict[str, Any], Any],
                  **{'runner': str,
                     'project': str,
                     'job_name': str,
                     'region': str,
                     'job_timestamp': str}
                  )
@with_output_types(IncarcerationMetric)
class ProduceIncarcerationMetric(beam.DoFn):
    """Produces IncarcerationMetrics."""

    def process(self, element, *args, **kwargs):
        """Converts an incarceration metric key into a IncarcerationMetric.

        The pipeline options are sent in as the **kwargs so that the job_id(pipeline_options) function can be called to
        retrieve the job_id.

        Args:
            element: A tuple containing the dictionary for a given, and a
                dictionary containing the values for the given metric.
            **kwargs: This should be a dictionary with values for the following keys:
                    - runner: Either 'DirectRunner' or 'DataflowRunner'
                    - project: GCP project ID
                    - job_name: Name of the pipeline job
                    - region: Region where the pipeline job is running
                    - job_timestamp: Timestamp for the current job, to be used if the job is running locally.

        Yields:
            The IncarcerationMetric.
        """
        pipeline_options = kwargs

        pipeline_job_id = job_id(pipeline_options)

        (dict_metric_key, value) = element

        if value is None:
            # Due to how the pipeline arrives at this function, this should be impossible.
            raise ValueError("No value associated with this metric key.")

        metric_type = dict_metric_key.get('metric_type')

        if dict_metric_key.get('person_id') is not None:
            # The count value for all person-level metrics should be 1
            value = 1

        dict_metric_key['count'] = value

        if metric_type == IncarcerationMetricType.ADMISSION:
            incarceration_metric = IncarcerationAdmissionMetric.build_from_metric_key_group(
                dict_metric_key, pipeline_job_id)
        elif metric_type == IncarcerationMetricType.POPULATION:
            incarceration_metric = IncarcerationPopulationMetric.build_from_metric_key_group(
                dict_metric_key, pipeline_job_id)
        elif metric_type == IncarcerationMetricType.RELEASE:
            incarceration_metric = IncarcerationReleaseMetric.build_from_metric_key_group(
                dict_metric_key, pipeline_job_id)
        else:
            logging.error("Unexpected metric of type: %s", dict_metric_key.get('metric_type'))
            return

        if incarceration_metric:
            yield incarceration_metric

    def to_runner_api_parameter(self, _):
        pass  # Passing unused abstract method.


@with_input_types(IncarcerationMetric)
@with_output_types(beam.typehints.Dict[str, Any])
class IncarcerationMetricWritableDict(beam.DoFn):
    """Builds a dictionary in the format necessary to write the output to BigQuery."""

    def process(self, element, *args, **kwargs):
        """The beam.io.WriteToBigQuery transform requires elements to be in dictionary form, where the values are in
        formats as required by BigQuery I/O connector.

        For a list of required formats, see the "Data types" section of:
            https://beam.apache.org/documentation/io/built-in/google-bigquery/

        Args:
            element: A ProgramMetric

        Yields:
            A dictionary representation of the ProgramMetric in the format Dict[str, Any] so that it can be written to
                BigQuery using beam.io.WriteToBigQuery.
        """
        element_dict = json_serializable_metric_key(element.__dict__)

        if isinstance(element, IncarcerationAdmissionMetric):
            yield beam.pvalue.TaggedOutput('admissions', element_dict)
        if isinstance(element, IncarcerationPopulationMetric):
            yield beam.pvalue.TaggedOutput('populations', element_dict)
        if isinstance(element, IncarcerationReleaseMetric):
            yield beam.pvalue.TaggedOutput('releases', element_dict)

    def to_runner_api_parameter(self, _):
        pass  # Passing unused abstract method.


def get_arg_parser() -> argparse.ArgumentParser:
    """Returns the parser for the command-line arguments for this pipeline."""
    parser = argparse.ArgumentParser()

    # Parse arguments
    add_shared_pipeline_arguments(parser, include_calculation_limit_args=True)

    parser.add_argument('--metric_types',
                        dest='metric_types',
                        type=str,
                        nargs='+',
                        choices=[
                            'ALL',
                            IncarcerationMetricType.ADMISSION.value,
                            IncarcerationMetricType.POPULATION.value,
                            IncarcerationMetricType.RELEASE.value
                        ],
                        help='A list of the types of metric to calculate.',
                        default={'ALL'})

    return parser


def run(apache_beam_pipeline_options: PipelineOptions,
        data_input: str,
        reference_input: str,
        output: str,
        calculation_month_count: int,
        metric_types: List[str],
        state_code: Optional[str],
        calculation_end_month: Optional[str],
        person_filter_ids: Optional[List[int]]):
    """Runs the incarceration calculation pipeline."""

    # Workaround to load SQLAlchemy objects at start of pipeline. This is necessary because the BuildRootEntity
    # function tries to access attributes of relationship properties on the SQLAlchemy room_schema_class before they
    # have been loaded. However, if *any* SQLAlchemy objects have been instantiated, then the relationship properties
    # are loaded and their attributes can be successfully accessed.
    _ = schema.StatePerson()

    apache_beam_pipeline_options.view_as(SetupOptions).save_main_session = True

    # Get pipeline job details
    all_apache_beam_pipeline_options = apache_beam_pipeline_options.get_all_options()

    query_dataset = all_apache_beam_pipeline_options['project'] + '.' + data_input
    reference_dataset = all_apache_beam_pipeline_options['project'] + '.' + reference_input

    person_id_filter_set = set(person_filter_ids) if person_filter_ids else None

    with beam.Pipeline(options=apache_beam_pipeline_options) as p:
        # Get StatePersons
        persons = (p | 'Load StatePersons' >>
                   BuildRootEntity(dataset=query_dataset, root_entity_class=entities.StatePerson,
                                   unifying_id_field=entities.StatePerson.get_class_id_name(),
                                   build_related_entities=True, unifying_id_field_filter_set=person_id_filter_set))

        # Get StateSentenceGroups
        sentence_groups = (p | 'Load StateSentenceGroups' >>
                           BuildRootEntity(
                               dataset=query_dataset,
                               root_entity_class=entities.StateSentenceGroup,
                               unifying_id_field=entities.StatePerson.get_class_id_name(),
                               build_related_entities=True,
                               unifying_id_field_filter_set=person_id_filter_set,
                               state_code=state_code
                           ))

        # Get StateIncarcerationSentences
        incarceration_sentences = (p | 'Load StateIncarcerationSentences' >>
                                   BuildRootEntity(
                                       dataset=query_dataset,
                                       root_entity_class=entities.StateIncarcerationSentence,
                                       unifying_id_field=entities.StatePerson.get_class_id_name(),
                                       build_related_entities=True,
                                       unifying_id_field_filter_set=person_id_filter_set,
                                       state_code=state_code
                                   ))

        # Get StateSupervisionSentences
        supervision_sentences = (p | 'Load StateSupervisionSentences' >>
                                 BuildRootEntity(
                                     dataset=query_dataset,
                                     root_entity_class=entities.StateSupervisionSentence,
                                     unifying_id_field=entities.StatePerson.get_class_id_name(),
                                     build_related_entities=True,
                                     unifying_id_field_filter_set=person_id_filter_set,
                                     state_code=state_code
                                 ))

        if state_code is None or state_code == 'US_MO':
            # Bring in the reference table that includes sentence status ranking information
            us_mo_sentence_status_query = f"SELECT * FROM `{reference_dataset}.us_mo_sentence_statuses`"

            us_mo_sentence_statuses = (p | "Read MO sentence status table from BigQuery" >>
                                       beam.io.Read(beam.io.BigQuerySource(query=us_mo_sentence_status_query,
                                                                           use_standard_sql=True)))
        else:
            us_mo_sentence_statuses = (p | f"Generate empty MO statuses list for non-MO state run: {state_code} " >>
                                       beam.Create([]))

        us_mo_sentence_status_rankings_as_kv = (
            us_mo_sentence_statuses |
            'Convert MO sentence status ranking table to KV tuples' >>
            beam.ParDo(ConvertDictToKVTuple(), 'person_id')
        )

        supervision_sentences_and_statuses = (
            {'incarceration_sentences': incarceration_sentences,
             'supervision_sentences': supervision_sentences,
             'sentence_statuses': us_mo_sentence_status_rankings_as_kv}
            | 'Group sentences to the sentence statuses for that person' >>
            beam.CoGroupByKey()
        )

        sentences_converted = (
            supervision_sentences_and_statuses
            | 'Convert to state-specific sentences' >>
            beam.ParDo(ConvertSentencesToStateSpecificType()).with_outputs('incarceration_sentences',
                                                                           'supervision_sentences')
        )

        sentences_and_sentence_groups = (
            {'sentence_groups': sentence_groups,
             'incarceration_sentences': sentences_converted.incarceration_sentences,
             'supervision_sentences': sentences_converted.supervision_sentences}
            | 'Group sentences to sentence groups' >>
            beam.CoGroupByKey()
        )

        # Set hydrated sentences on the corresponding sentence groups
        sentence_groups_with_hydrated_sentences = (
            sentences_and_sentence_groups | 'Set hydrated sentences on sentence groups' >>
            beam.ParDo(SetSentencesOnSentenceGroup())
        )

        # Group each StatePerson with their related entities
        person_and_sentence_groups = (
            {'person': persons,
             'sentence_groups': sentence_groups_with_hydrated_sentences}
            | 'Group StatePerson to SentenceGroups' >>
            beam.CoGroupByKey()
        )

        # Bring in the table that associates people and their county of residence
        person_id_to_county_query = \
            f"SELECT * FROM `{reference_dataset}.persons_to_recent_county_of_residence`"

        person_id_to_county_kv = (
            p | "Read person_id to county associations from BigQuery" >>
            beam.io.Read(beam.io.BigQuerySource(
                query=person_id_to_county_query,
                use_standard_sql=True))
            | "Convert person_id to county association table to KV" >>
            beam.ParDo(ConvertDictToKVTuple(), 'person_id')
        )

        # Identify IncarcerationEvents events from the StatePerson's StateIncarcerationPeriods
        person_events = (person_and_sentence_groups | 'Classify Incarceration Events' >>
                         beam.ParDo(ClassifyIncarcerationEvents(), AsDict(person_id_to_county_kv)))

        # Get pipeline job details for accessing job_id
        all_pipeline_options = apache_beam_pipeline_options.get_all_options()

        # Add timestamp for local jobs
        job_timestamp = datetime.datetime.now().strftime('%Y-%m-%d_%H_%M_%S.%f')
        all_apache_beam_pipeline_options['job_timestamp'] = job_timestamp

        # Get the type of metric to calculate
        metric_types_set = set(metric_types)

        # Get IncarcerationMetrics
        incarceration_metrics = (person_events | 'Get Incarceration Metrics' >>
                                 GetIncarcerationMetrics(
                                     pipeline_options=all_pipeline_options,
                                     metric_types=metric_types_set,
                                     calculation_end_month=calculation_end_month,
                                     calculation_month_count=calculation_month_count))

        if person_id_filter_set:
            logging.warning("Non-empty person filter set - returning before writing metrics.")
            return

        # Convert the metrics into a format that's writable to BQ
        writable_metrics = (incarceration_metrics | 'Convert to dict to be written to BQ' >>
                            beam.ParDo(IncarcerationMetricWritableDict()).with_outputs(
                                'admissions', 'populations', 'releases'))

        # Write the metrics to the output tables in BigQuery
        admissions_table = output + '.incarceration_admission_metrics'

        population_table = output + '.incarceration_population_metrics'

        releases_table = output + '.incarceration_release_metrics'

        _ = (writable_metrics.admissions
             | f"Write admission metrics to BQ table: {admissions_table}" >>
             beam.io.WriteToBigQuery(
                 table=admissions_table,
                 create_disposition=beam.io.BigQueryDisposition.CREATE_NEVER,
                 write_disposition=beam.io.BigQueryDisposition.WRITE_APPEND
             ))

        _ = (writable_metrics.populations
             | f"Write population metrics to BQ table: {population_table}" >>
             beam.io.WriteToBigQuery(
                 table=population_table,
                 create_disposition=beam.io.BigQueryDisposition.CREATE_NEVER,
                 write_disposition=beam.io.BigQueryDisposition.WRITE_APPEND
             ))

        _ = (writable_metrics.releases
             | f"Write release metrics to BQ table: {releases_table}" >>
             beam.io.WriteToBigQuery(
                 table=releases_table,
                 create_disposition=beam.io.BigQueryDisposition.CREATE_NEVER,
                 write_disposition=beam.io.BigQueryDisposition.WRITE_APPEND
             ))
