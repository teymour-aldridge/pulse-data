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
"""Tests for incarceration/incarceration_event.py."""
from recidiviz.calculator.pipeline.incarceration.incarceration_event import \
    IncarcerationEvent, IncarcerationAdmissionEvent, IncarcerationReleaseEvent
from recidiviz.common.constants.state.state_incarceration_period import \
    StateIncarcerationPeriodAdmissionReason, \
    StateIncarcerationPeriodReleaseReason


def test_incarceration_event():
    state_code = 'CA'
    year = 2000
    month = 11
    facility = 'FACILITY D'

    incarceration_event = IncarcerationEvent(
        state_code, year, month, facility)

    assert incarceration_event.state_code == state_code
    assert incarceration_event.year == year
    assert incarceration_event.month == month
    assert incarceration_event.facility == facility


def test_incarceration_admission_event():
    state_code = 'CA'
    year = 2000
    month = 11
    facility = 'PRISON V'
    admission_reason = StateIncarcerationPeriodAdmissionReason.NEW_ADMISSION

    incarceration_event = IncarcerationAdmissionEvent(
        state_code, year, month, facility, admission_reason
    )

    assert incarceration_event.state_code == state_code
    assert incarceration_event.year == year
    assert incarceration_event.month == month
    assert incarceration_event.facility == facility
    assert incarceration_event.admission_reason == admission_reason


def test_incarceration_release_event():
    state_code = 'CA'
    year = 2000
    month = 11
    facility = 'PRISON V'
    release_reason = StateIncarcerationPeriodReleaseReason.SENTENCE_SERVED

    incarceration_event = IncarcerationReleaseEvent(
        state_code, year, month, facility, release_reason
    )

    assert incarceration_event.state_code == state_code
    assert incarceration_event.year == year
    assert incarceration_event.month == month
    assert incarceration_event.facility == facility
    assert incarceration_event.release_reason == release_reason


def test_eq_different_field():
    state_code = 'CA'
    year = 2000
    month = 11
    facility = 'HELLO'

    first = IncarcerationEvent(state_code, year, month, facility)

    second = IncarcerationEvent(state_code, year, month, 'DIFFERENT')

    assert first != second


def test_eq_different_types():
    state_code = 'CA'
    year = 2000
    month = 11
    facility = 'PRISON V'
    admission_reason = StateIncarcerationPeriodAdmissionReason.NEW_ADMISSION

    incarceration_event = IncarcerationAdmissionEvent(
        state_code, year, month, facility, admission_reason
    )

    different = "Everything you do is a banana"

    assert incarceration_event != different