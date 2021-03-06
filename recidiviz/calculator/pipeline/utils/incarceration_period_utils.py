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
"""Utils for validating and manipulating incarceration periods for use in
calculations."""

import logging
from copy import deepcopy

from datetime import date
from functools import cmp_to_key
from typing import List

from recidiviz.calculator.pipeline.utils.state_calculation_config_manager import \
    temporary_custody_periods_under_state_authority, non_prison_periods_under_state_authority
from recidiviz.common.constants.state.state_incarceration import StateIncarcerationType
from recidiviz.persistence.entity.state.entities import StateIncarcerationPeriod
from recidiviz.common.constants.state.state_incarceration_period import \
    StateIncarcerationPeriodAdmissionReason as AdmissionReason, \
    StateIncarcerationPeriodStatus, StateIncarcerationPeriodReleaseReason
from recidiviz.common.constants.state.state_incarceration_period import \
    StateIncarcerationPeriodReleaseReason as ReleaseReason
from recidiviz.persistence.entity.entity_utils import is_placeholder, get_single_state_code


def drop_placeholder_periods(
        incarceration_periods: List[StateIncarcerationPeriod]) -> List[StateIncarcerationPeriod]:
    """Removes any incarceration periods that are placeholders. Returns the valid incarceration periods."""
    return [ip for ip in incarceration_periods if not is_placeholder(ip)]


def validate_admission_data(
        incarceration_periods: List[StateIncarcerationPeriod]) -> \
        List[StateIncarcerationPeriod]:
    """Removes any incarceration periods that don't have admission dates
    or admission reasons.

    Returns the valid incarceration periods.
    """
    validated_incarceration_periods: List[StateIncarcerationPeriod] = []

    for incarceration_period in incarceration_periods:
        if is_placeholder(incarceration_period):
            # Drop any placeholder incarceration periods from the calculations
            continue
        if not incarceration_period.admission_date:
            logging.info("No admission_date on incarceration period with"
                         " id: %d",
                         incarceration_period.incarceration_period_id)
            continue
        if not incarceration_period.admission_reason:
            logging.info("No admission_reason on incarceration period with"
                         " id: %d",
                         incarceration_period.incarceration_period_id)
            continue

        validated_incarceration_periods.append(incarceration_period)

    return validated_incarceration_periods


def validate_release_data(
        incarceration_periods: List[StateIncarcerationPeriod]) -> \
        List[StateIncarcerationPeriod]:
    """Removes any incarceration periods that don't have release dates
    or release reasons. Removes release data if the release date is in the
    future.

    Returns the valid incarceration periods.
    """
    validated_incarceration_periods: List[StateIncarcerationPeriod] = []

    for incarceration_period in incarceration_periods:
        if not incarceration_period.release_date and \
                incarceration_period.status != \
                StateIncarcerationPeriodStatus.IN_CUSTODY:
            logging.info("No release_date on intermediate incarceration "
                         "period with id: %d",
                         incarceration_period.incarceration_period_id)
            continue
        if not incarceration_period.release_reason and \
                incarceration_period.status != \
                StateIncarcerationPeriodStatus.IN_CUSTODY:
            logging.info("No release_reason on intermediate incarceration "
                         "period with id: %d",
                         incarceration_period.incarceration_period_id)
            incarceration_period.release_reason = StateIncarcerationPeriodReleaseReason.INTERNAL_UNKNOWN

        if not incarceration_period.release_date and \
                (incarceration_period.release_reason or incarceration_period.release_reason_raw_text):
            # TODO(2981): Update MO ingest to pull in status date of last TAK026 status to use when the release date is
            #  99999999. This should eliminate the 600ish instances of this we're seeing.
            logging.warning("No release_date for incarceration period (%d) with nonnull release_reason (%s) or "
                            "release_reason_raw_text (%s)",
                            incarceration_period.incarceration_period_id,
                            incarceration_period.release_reason,
                            incarceration_period.release_reason_raw_text)
            continue
        if incarceration_period.release_date is not None and \
                incarceration_period.release_date > date.today():
            # If the person has not been released yet, remove the release
            # date and release reason, and set the status to be in custody
            incarceration_period.release_date = None
            incarceration_period.release_reason = None
            incarceration_period.status = \
                StateIncarcerationPeriodStatus.IN_CUSTODY

        validated_incarceration_periods.append(incarceration_period)

    return validated_incarceration_periods


def collapse_incarceration_period_transfers(
        sorted_incarceration_periods: List[StateIncarcerationPeriod]) -> List[StateIncarcerationPeriod]:
    """Collapses any incarceration periods that are connected by transfers.

    Loops through all of the StateIncarcerationPeriods and combines adjacent
    periods that are connected by a transfer. Only connects two periods if the
    release reason of the first is `TRANSFER` and the admission reason for the
    second is also `TRANSFER`.

    Args:
        sorted_incarceration_periods: list of StateIncarcerationPeriods for a StatePerson, sorted by ascending
            admission_date

    Returns:
        A list of collapsed StateIncarcerationPeriods.
    """

    new_incarceration_periods: List[StateIncarcerationPeriod] = []
    open_transfer = False

    # TODO(1782): Check to see if back to back incarceration periods are related
    #  to the same StateIncarcerationSentence or SentenceGroup to be sure we
    #  aren't counting stacked sentences or related periods as recidivism.
    for incarceration_period in sorted_incarceration_periods:
        if open_transfer:
            if incarceration_period.admission_reason == AdmissionReason.TRANSFER:
                # If there is an open transfer period and they were
                # transferred into this incarceration period, then combine this
                # period with the open transfer period.
                start_period = new_incarceration_periods.pop(-1)
                combined_period = combine_incarceration_periods(start_period, incarceration_period)
                new_incarceration_periods.append(combined_period)
            else:
                # They weren't transferred here. Add this as a new
                # incarceration period.
                # TODO(1790): Analyze how often a transfer out is followed by an
                #  admission type that isn't a transfer to ensure we aren't
                #  making bad assumptions with this transfer logic.
                new_incarceration_periods.append(incarceration_period)
        else:
            # TODO(1790): Analyze how often an incarceration period that starts
            #  with a transfer in is not preceded by a transfer out of a
            #  different facility.
            new_incarceration_periods.append(incarceration_period)

        # If this incarceration period ended in a transfer, then flag
        # that there's an open transfer period.
        open_transfer = (incarceration_period.release_reason == ReleaseReason.TRANSFER)

    return new_incarceration_periods


def collapse_temporary_custody_and_revocation_periods(
        sorted_incarceration_periods: List[StateIncarcerationPeriod]) -> List[StateIncarcerationPeriod]:
    """Looks through the |sorted_incarceration_periods| and collapses consecutive periods only if the first period
    has a temporary custody admission reason and the subsequent period has a revocation admission reason. When the
    periods are collapsed, the revocation admission reason is kept over the temporary custody admission reason.

    This method assumes the input |sorted_incarceration_periods| are sorted by ascending admission reason.
    """

    previous_period = None
    collapsed_ips = []
    for incarceration_period in sorted_incarceration_periods:
        if not previous_period:
            previous_period = incarceration_period
            continue

        if previous_period.release_date == incarceration_period.admission_date \
            and previous_period.admission_reason == AdmissionReason.TEMPORARY_CUSTODY \
            and incarceration_period.admission_reason in [
                    AdmissionReason.DUAL_REVOCATION,
                    AdmissionReason.PAROLE_REVOCATION,
                    AdmissionReason.PROBATION_REVOCATION]:
            merged_period = combine_incarceration_periods(
                previous_period, incarceration_period, overwrite_admission_reason=True)
            collapsed_ips.append(merged_period)
            previous_period = None
        else:
            collapsed_ips.append(previous_period)
            previous_period = incarceration_period

    if previous_period:
        collapsed_ips.append(previous_period)

    return collapsed_ips


def combine_incarceration_periods(start: StateIncarcerationPeriod,
                                  end: StateIncarcerationPeriod,
                                  overwrite_admission_reason: bool = False) -> StateIncarcerationPeriod:
    """Combines two StateIncarcerationPeriods.

    Brings together two StateIncarcerationPeriods by setting the following
    fields on a deep copy of the |start| StateIncarcerationPeriod to the values
    on the |end| StateIncarcerationPeriod:

        [status, release_date, facility, housing_unit, facility_security_level,
        facility_security_level_raw_text, projected_release_reason,
        projected_release_reason_raw_text, release_reason,
        release_reason_raw_text]

        Args:
            start: The starting StateIncarcerationPeriod.
            end: The ending StateIncarcerationPeriod.
            overwrite_admission_reason: Whether to use the end admission reason instead of the start admission reason.
    """

    collapsed_incarceration_period = deepcopy(start)

    if overwrite_admission_reason:
        collapsed_incarceration_period.admission_reason = end.admission_reason
        collapsed_incarceration_period.admission_reason_raw_text = end.admission_reason_raw_text

    collapsed_incarceration_period.status = end.status
    collapsed_incarceration_period.release_date = end.release_date
    collapsed_incarceration_period.facility = end.facility
    collapsed_incarceration_period.housing_unit = end.housing_unit
    collapsed_incarceration_period.facility_security_level = end.facility_security_level
    collapsed_incarceration_period.facility_security_level_raw_text = end.facility_security_level_raw_text
    collapsed_incarceration_period.projected_release_reason = end.projected_release_reason
    collapsed_incarceration_period.projected_release_reason_raw_text = end.projected_release_reason_raw_text
    collapsed_incarceration_period.release_reason = end.release_reason
    collapsed_incarceration_period.release_reason_raw_text = end.release_reason_raw_text

    return collapsed_incarceration_period


def prepare_incarceration_periods_for_calculations(
        incarceration_periods: List[StateIncarcerationPeriod],
        collapse_temporary_custody_periods_with_revocation: bool = False,
        collapse_transfers: bool = True,
) -> List[StateIncarcerationPeriod]:
    """Validates, sorts, and collapses the incarceration period inputs.

    Ensures the necessary dates and fields are set on each incarceration period. If an incarceration period is found
    with missing data, drops the incarceration period from the calculations. Then, sorts the list of valid
    StateIncarcerationPeriods by admission_date, and collapses the ones connected by a transfer.
    """

    updated_periods = _filter_and_update_incarceration_periods_for_calculations(incarceration_periods)

    # First sort by release date. Any periods without release dates will be at the end.
    updated_periods.sort(key=lambda b: (b.release_date is None, b.release_date))

    # Then sort by admission date. All admission dates will be set by this point. Python sort is a stable sort so
    # periods with the same admission date will be sorted by release date.
    updated_periods.sort(key=lambda b: b.admission_date)

    collapsed_periods = _collapse_incarceration_periods_for_calculations(
        updated_periods, collapse_temporary_custody_periods_with_revocation, collapse_transfers)
    return collapsed_periods


def _filter_and_update_incarceration_periods_for_calculations(
        incarceration_periods: List[StateIncarcerationPeriod]) -> List[StateIncarcerationPeriod]:
    """Returns a modified and filtered subset of the provided |incarceration_periods| list so that all remaining
    periods have the the fields necessary for calculations.
    """
    if not incarceration_periods:
        return []

    filtered_incarceration_periods = drop_placeholder_periods(incarceration_periods)

    filtered_incarceration_periods = _infer_missing_dates_and_statuses(filtered_incarceration_periods)

    filtered_incarceration_periods = drop_periods_not_under_state_custodial_authority(filtered_incarceration_periods)

    filtered_incarceration_periods = validate_admission_data(filtered_incarceration_periods)

    filtered_incarceration_periods = validate_release_data(filtered_incarceration_periods)

    return filtered_incarceration_periods


def _sort_ips_by_set_dates_and_statuses(incarceration_periods: List[StateIncarcerationPeriod]):
    """Sorts incarceration periods chronologically by the admission and release dates according to this logic:
        - Sorts by admission_date, if set, else by release_date
        - For periods with the same admission_date:
            - If neither have a release_date, sorts by custody status
            - Else, sorts by release_date, with unset release_dates before set release_dates
    """
    def _sort_by_external_id(ip_a: StateIncarcerationPeriod, ip_b: StateIncarcerationPeriod) -> int:
        if ip_a.external_id is None or ip_b.external_id is None:
            raise ValueError("Expect no placeholder periods in this function.")

        # Alphabetic sort by external_id
        return -1 if ip_a.external_id < ip_b.external_id else 1

    def _sort_by_nonnull_release_dates(ip_a: StateIncarcerationPeriod, ip_b: StateIncarcerationPeriod) -> int:
        if not ip_a.release_date or not ip_b.release_date:
            raise ValueError('Expected nonnull release dates')
        if ip_a.release_date != ip_b.release_date:
            return (ip_a.release_date - ip_b.release_date).days
        # They have the same admission and release dates. Sort by external_id.
        return _sort_by_external_id(ip_a, ip_b)

    def _sort_by_custody_status(ip_a: StateIncarcerationPeriod, ip_b: StateIncarcerationPeriod) -> int:
        normalized_status_a = (StateIncarcerationPeriodStatus.IN_CUSTODY
                               if ip_a.status == StateIncarcerationPeriodStatus.IN_CUSTODY
                               else StateIncarcerationPeriodStatus.NOT_IN_CUSTODY)
        normalized_status_b = (StateIncarcerationPeriodStatus.IN_CUSTODY
                               if ip_b.status == StateIncarcerationPeriodStatus.IN_CUSTODY
                               else StateIncarcerationPeriodStatus.NOT_IN_CUSTODY)
        if normalized_status_a == normalized_status_b:
            return _sort_by_external_id(ip_a, ip_b)
        # Sort by custody status. Order IN_CUSTODY after all other statuses.
        if normalized_status_a == StateIncarcerationPeriodStatus.IN_CUSTODY:
            return 1
        if normalized_status_b == StateIncarcerationPeriodStatus.IN_CUSTODY:
            return -1
        raise ValueError('One status should have IN_CUSTODY at this point')

    def _sort_equal_admission_date(ip_a: StateIncarcerationPeriod, ip_b: StateIncarcerationPeriod) -> int:
        if ip_a.admission_date != ip_b.admission_date:
            raise ValueError('Expected equal admission dates')
        if ip_a.release_date and ip_b.release_date:
            return _sort_by_nonnull_release_dates(ip_a, ip_b)
        if ip_a.admission_date is None or ip_b.admission_date is None:
            raise ValueError(
                'Admission reasons expected to be equal and nonnull at this point otherwise we would have a'
                'period that has a null release and null admission reason.')
        if ip_a.release_date is None and ip_b.release_date is None:
            return _sort_by_custody_status(ip_a, ip_b)
        # Sort by release dates, with unset release dates coming first if the following period is greater than 0 days
        # long (we assume in this case that we forgot to close this open period).
        if ip_a.release_date:
            return 1 if (ip_a.release_date - ip_a.admission_date).days else -1
        if ip_b.release_date:
            return -1 if (ip_b.release_date - ip_b.admission_date).days else 1
        raise ValueError("At least one of the periods is expected to have a release_date at this point.")

    def _sort_function(ip_a: StateIncarcerationPeriod, ip_b: StateIncarcerationPeriod) -> int:
        if ip_a.admission_date == ip_b.admission_date:
            return _sort_equal_admission_date(ip_a, ip_b)

        # Sort by admission_date, if set, or release_date if not set
        date_a = ip_a.admission_date if ip_a.admission_date else ip_a.release_date
        date_b = ip_b.admission_date if ip_b.admission_date else ip_b.release_date
        if not date_a:
            raise ValueError(f'Found period with no admission or release date {ip_a}')
        if not date_b:
            raise ValueError(f'Found period with no admission or release date {ip_b}')
        if date_a == date_b:
            if ip_a.release_date and ip_b.release_date:
                # These share a release date. Sort by external_id.
                return _sort_by_external_id(ip_a, ip_b)

            # One has an admission date and the other has a release date on the same day. Order the admission before
            # the release.
            return -1 if ip_a.admission_date else 1
        return (date_a - date_b).days

    incarceration_periods.sort(key=cmp_to_key(_sort_function))


def _infer_missing_dates_and_statuses(incarceration_periods: List[StateIncarcerationPeriod]) -> \
        List[StateIncarcerationPeriod]:
    """First, sorts the incarceration_periods in chronological order of the admission and release dates. Then, for any
    periods missing dates and statuses, infers this information given the other incarceration periods.
    """
    _sort_ips_by_set_dates_and_statuses(incarceration_periods)

    for index, ip in enumerate(incarceration_periods):
        if ip.release_date is None:
            if index == len(incarceration_periods) - 1:
                # This is the last incarceration period in the list.
                if ip.status != StateIncarcerationPeriodStatus.IN_CUSTODY:
                    # If the person is no longer in custody on this period, set the release date to the admission date.
                    ip.release_date = ip.admission_date
                    ip.release_reason = ReleaseReason.INTERNAL_UNKNOWN
            else:
                # This is not the last incarceration period in the list. Set the release date to the next admission or
                # release date.
                next_ip = incarceration_periods[index + 1]

                ip.release_date = next_ip.admission_date if next_ip.admission_date else next_ip.release_date

                if ip.release_reason is None:
                    if next_ip.admission_reason == AdmissionReason.TRANSFER:
                        # If they were transferred into the next period, infer that this release was a transfer
                        ip.release_reason = ReleaseReason.TRANSFER
                    else:
                        # We have no idea what this release reason was. Set as INTERNAL_UNKNOWN.
                        ip.release_reason = ReleaseReason.INTERNAL_UNKNOWN

                ip.status = StateIncarcerationPeriodStatus.NOT_IN_CUSTODY

        if ip.admission_date is None:
            if index == 0:
                # If the admission date is not set, and this is the first incarceration period, then set the
                # admission_date to be the same as the release_date
                ip.admission_date = ip.release_date
                ip.admission_reason = AdmissionReason.INTERNAL_UNKNOWN
            else:
                # If the admission date is not set, and this is not the first incarceration period, then set the
                # admission_date to be the same as the release_date or admission_date of the preceding period
                previous_ip = incarceration_periods[index - 1]
                ip.admission_date = previous_ip.release_date if previous_ip.release_date else ip.admission_date

                if ip.admission_reason is None:
                    if previous_ip.release_reason == ReleaseReason.TRANSFER:
                        # If they were transferred out of the previous period, infer that this admission was a transfer
                        ip.admission_reason = AdmissionReason.TRANSFER
                    else:
                        # We have no idea what this release reason was. Set as INTERNAL_UNKNOWN.
                        ip.admission_reason = AdmissionReason.INTERNAL_UNKNOWN

    return incarceration_periods


def drop_periods_not_under_state_custodial_authority(incarceration_periods: List[StateIncarcerationPeriod]) \
        -> List[StateIncarcerationPeriod]:
    """Returns a filtered subset of the provided |incarceration_periods| where all periods that are not under state
    custodial authority are filtered out.
    """
    # TODO(2912): Use `custodial_authority` to determine this instead, when that field exists on incarceration periods.
    state_code = get_single_state_code(incarceration_periods)
    filtered_incarceration_periods = incarceration_periods
    if not temporary_custody_periods_under_state_authority(state_code):
        filtered_incarceration_periods = drop_temporary_custody_periods(filtered_incarceration_periods)
    if not non_prison_periods_under_state_authority(state_code):
        filtered_incarceration_periods = _drop_non_prison_periods(filtered_incarceration_periods)
    return filtered_incarceration_periods


def drop_temporary_custody_periods(incarceration_periods: List[StateIncarcerationPeriod])\
        -> List[StateIncarcerationPeriod]:
    """Removes any incarceration periods that denote an admission to a temporary custody.

    Returns the filtered incarceration periods.
    """

    return [ip for ip in incarceration_periods if ip.admission_reason != AdmissionReason.TEMPORARY_CUSTODY]


def _drop_non_prison_periods(incarceration_periods: List[StateIncarcerationPeriod]) -> List[StateIncarcerationPeriod]:
    """Removes any incarceration periods where the incarceration type isn't STATE_PRISON.

    Returns the filtered incarceration periods.
    """
    return [ip for ip in incarceration_periods if ip.incarceration_type == StateIncarcerationType.STATE_PRISON]


def _collapse_incarceration_periods_for_calculations(
        sorted_incarceration_periods: List[StateIncarcerationPeriod],
        collapse_temporary_custody_periods_with_revocation: bool,
        collapse_transfers: bool) -> List[StateIncarcerationPeriod]:
    """Collapses the provided |sorted_incarceration_periods| based on the input params
    |collapse_temporary_custody_periods_with_revocation| and |collapse_transfers|. Assumes the
    |sorted_incarceration_periods| are sorted based on ascending admission_date.
    """
    collapsed_periods = sorted_incarceration_periods
    if collapse_transfers:
        collapsed_periods = collapse_incarceration_period_transfers(collapsed_periods)

    if collapse_temporary_custody_periods_with_revocation:
        collapsed_periods = collapse_temporary_custody_and_revocation_periods(collapsed_periods)

    return collapsed_periods
