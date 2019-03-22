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
"""Contains logic for communicating with the persistence layer."""
import datetime
import logging
import os
from distutils.util import strtobool  # pylint: disable=no-name-in-module
from typing import List, Tuple

from opencensus.stats import aggregation
from opencensus.stats import measure
from opencensus.stats import view

from recidiviz import Session
from recidiviz.common.constants.bond import BondStatus
from recidiviz.common.constants.booking import CustodyStatus
from recidiviz.common.constants.charge import ChargeStatus
from recidiviz.common.constants.entity_enum import EnumParsingError
from recidiviz.common.constants.hold import HoldStatus
from recidiviz.common.constants.person import PROTECTED_CLASSES
from recidiviz.common.constants.sentence import SentenceStatus
from recidiviz.common.ingest_metadata import IngestMetadata
from recidiviz.ingest.scrape.constants import MAX_PEOPLE_TO_LOG
from recidiviz.persistence import entity_matching, entities, \
    persistence_utils, validator
from recidiviz.persistence.converter import converter
from recidiviz.persistence.database import database
from recidiviz.utils import environment, monitoring

m_people = measure.MeasureInt("persistence/num_people",
                              "The number of people persisted", "1")
m_errors = measure.MeasureInt("persistence/num_errors",
                              "The number of errors", "1")
people_persisted_view = view.View("recidiviz/persistence/num_people",
                                  "The sum of people persisted",
                                  [monitoring.TagKey.REGION,
                                   monitoring.TagKey.PERSISTED],
                                  m_people,
                                  aggregation.SumAggregation())
errors_persisted_view = view.View("recidiviz/persistence/num_errors",
                                  "The sum of errors in the persistence layer",
                                  [monitoring.TagKey.REGION,
                                   monitoring.TagKey.ERROR],
                                  m_errors,
                                  aggregation.SumAggregation())
monitoring.register_views([people_persisted_view, errors_persisted_view])

ERROR_THRESHOLD = 0.5


def infer_release_on_open_bookings(
        region_code: str, last_ingest_time: datetime.datetime,
        custody_status: CustodyStatus):
    """
   Look up all open bookings whose last_seen_time is earlier than the
   provided last_ingest_time in the provided region, update those
   bookings to have an inferred release date equal to the provided
   last_ingest_time.

   Args:
       region_code: the region_code
       last_ingest_time: The last time complete data was ingested for this
           region. In the normal ingest pipeline, this is the last start time
           of a background scrape for the region.
       custody_status: The custody status to be marked on the found open
           bookings. Defaults to INFERRED_RELEASE
   """

    session = Session()
    try:
        logging.info('Reading all bookings that happened before %s',
                     last_ingest_time)
        people = database.read_people_with_open_bookings_scraped_before_time(
            session, region_code, last_ingest_time)
        logging.info(
            'Found %s people with bookings that will be inferred released',
            len(people))
        for person in people:
            persistence_utils.remove_pii_for_person(person)
            _infer_release_date_for_bookings(person.bookings, last_ingest_time,
                                             custody_status)
        # TODO(#1172): Only pass in last_seen_time to write_people instead of
        # dummy object with half-filled values
        database.write_people(session, people, IngestMetadata(
            region=region_code, jurisdiction_id='',
            ingest_time=last_ingest_time))
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _infer_release_date_for_bookings(
        bookings: List[entities.Booking],
        last_ingest_time: datetime.datetime, custody_status: CustodyStatus):
    """Marks the provided bookings with an inferred release date equal to the
    provided date. Updates the custody_status to the provided custody
    status. Also updates all children of the updated booking to have status
    'REMOVED_WITHOUT_INFO"""

    for booking in bookings:
        if persistence_utils.is_booking_active(booking):
            logging.info('Marking booking %s as inferred release')
            booking.release_date = last_ingest_time.date()
            booking.release_date_inferred = True
            booking.custody_status = custody_status
            booking.custody_status_raw_text = None
            _mark_children_removed_from_source(booking)


def _mark_children_removed_from_source(booking: entities.Booking):
    """Marks all children of a booking with the status 'REMOVED_FROM_SOURCE'"""
    for hold in booking.holds:
        hold.status = HoldStatus.REMOVED_WITHOUT_INFO
        hold.status_raw_text = None

    for charge in booking.charges:
        charge.status = ChargeStatus.REMOVED_WITHOUT_INFO
        charge.status_raw_text = None
        if charge.sentence:
            charge.sentence.status = SentenceStatus.REMOVED_WITHOUT_INFO
            charge.sentence.status_raw_text = None
        if charge.bond:
            charge.bond.status = BondStatus.REMOVED_WITHOUT_INFO
            charge.bond.status_raw_text = None


def _should_persist():
    return bool(environment.in_gae() or \
                strtobool((os.environ.get('PERSIST_LOCALLY', 'false'))))


def _convert_and_count_errors(ingest_info, metadata):
    people = []
    protected_class_errors = 0
    enum_parsing_errors = 0
    ii_converter = converter.Converter(ingest_info, metadata)
    while not ii_converter.is_complete():
        try:
            people.append(ii_converter.convert_and_pop())
        except EnumParsingError as e:
            logging.error(str(e))
            if e.entity_type in PROTECTED_CLASSES:
                protected_class_errors += 1
            else:
                enum_parsing_errors += 1
        except Exception as e:
            logging.error(str(e))
            enum_parsing_errors += 1
    return people, enum_parsing_errors, protected_class_errors


def _should_abort(
        total_people,
        enum_parsing_errors=0,
        entity_matching_errors=0,
        protected_class_errors=0,
        data_validation_errors=0):
    # TODO: finalize the logic in here.
    if protected_class_errors:
        logging.error(
            'Aborting because there was an error regarding a protected class')
        return True
    if (enum_parsing_errors + entity_matching_errors +
            data_validation_errors) / total_people >= ERROR_THRESHOLD:
        logging.error(
            'Aborting because we exceeded the error threshold of %s with %s '
            'enum_parsing errors, %s entity_matching_errors, and %s '
            'data_validation_errors', ERROR_THRESHOLD, enum_parsing_errors,
            entity_matching_errors, data_validation_errors)
        return True
    return False


# Note: If we ever want to validate more than the existence of multiple open
# bookings, we should make validation an entirely separate module/step.
def validate_one_open_booking(people: List[entities.Person]) -> \
        Tuple[List[entities.Person], int]:
    data_validation_errors = 0
    validated_people = []
    for person in people:
        open_bookings = [booking for booking in person.bookings if
                         persistence_utils.is_booking_active(booking)]
        if len(open_bookings) > 1:
            logging.error(
                'Multiple open bookings found for person: %s', person.person_id)
            data_validation_errors += 1
        else:
            validated_people.append(person)
    return validated_people, data_validation_errors


def write(ingest_info, metadata):
    """
    If in prod or if 'PERSIST_LOCALLY' is set to true, persist each person in
    the ingest_info. If a person with the given surname/birthday already exists,
    then update that person.

    Otherwise, simply log the given ingest_infos for debugging
    """
    validator.validate(ingest_info)

    mtags = {monitoring.TagKey.SHOULD_PERSIST: _should_persist()}
    total_people = len(ingest_info.people)
    with monitoring.measurements(mtags) as measurements:

        # Convert the people one at a time and count the errors as they happen.
        people, enum_parsing_errors, protected_class_errors = \
            _convert_and_count_errors(ingest_info, metadata)
        people, data_validation_errors = validate_one_open_booking(people)
        logging.info('Converted %s people with %s enum_parsing_errors, %s'
                     ' protected_class_errors and %s data_validation_errors, '
                     '(logging max %d people):',
                     len(people), enum_parsing_errors, protected_class_errors,
                     data_validation_errors, MAX_PEOPLE_TO_LOG)
        loop_count = min(len(people), MAX_PEOPLE_TO_LOG)
        for i in range(loop_count):
            logging.info(people[i])
        measurements.measure_int_put(m_people, len(people))

        if _should_abort(
                total_people=total_people,
                enum_parsing_errors=enum_parsing_errors,
                protected_class_errors=protected_class_errors,
                data_validation_errors=data_validation_errors):
            return False

        if not _should_persist():
            return True

        persisted = False
        session = Session()
        try:
            logging.info('Starting entity matching')
            entity_matching_output = entity_matching.match(
                session, metadata.region, people)
            people = entity_matching_output.people
            # entity_matching_errors, orphaned_entities = \
            logging.info(
                'Completed entity matching with %s errors',
                entity_matching_output.error_count)
            if _should_abort(
                    total_people=total_people,
                    enum_parsing_errors=enum_parsing_errors,
                    entity_matching_errors=entity_matching_output.error_count,
                    data_validation_errors=data_validation_errors):
                return False
            database.write_people(
                session, people, metadata,
                orphaned_entities=entity_matching_output.orphaned_entities)
            logging.info('Successfully wrote to the database')
            session.commit()
            persisted = True
        except Exception as e:
            # Record the error type that happened and increment the counter
            mtags[monitoring.TagKey.ERROR] = type(e).__name__
            measurements.measure_int_put(m_errors, 1)
            session.rollback()
            raise
        finally:
            session.close()
            mtags[monitoring.TagKey.PERSISTED] = persisted
        return persisted
