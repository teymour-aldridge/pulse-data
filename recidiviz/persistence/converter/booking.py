# Recidiviz - a platform for tracking granular recidivism metrics in real time
# Copyright (C) 2018 Recidiviz, Inc.
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
# ============================================================================
"""Converts an ingest_info proto Booking to a persistence entity."""
from recidiviz.common.constants.booking import ReleaseReason, CustodyStatus, \
    Classification
from recidiviz.persistence.converter.converter_utils import fn, normalize, \
    parse_date, parse_external_id


def copy_fields_to_builder(booking_builder, proto, metadata):
    """Mutates the provided |booking_builder| by converting an ingest_info proto
     Booking.

     Note: This will not copy children into the Builder!
     """
    new = booking_builder

    new.external_id = fn(parse_external_id, 'booking_id', proto)
    new.admission_date = fn(parse_date, 'admission_date', proto)
    new.release_date, new.release_date_inferred = _parse_release_date(proto)
    new.projected_release_date = fn(parse_date, 'projected_release_date', proto)
    new.release_reason = fn(ReleaseReason.from_str, 'release_reason', proto,
                            metadata.enum_overrides)
    new.custody_status = fn(
        CustodyStatus.from_str, 'custody_status', proto,
        metadata.enum_overrides, default=CustodyStatus.IN_CUSTODY)
    new.facility = fn(normalize, 'facility', proto)
    new.classification = fn(Classification.from_str, 'classification', proto)

    new.last_seen_time = metadata.last_seen_time

    # TODO(#363): Add logic for the following fields
    new.admission_date_inferred = None


def _parse_release_date(proto):
    release_date = fn(parse_date, 'release_date', proto)
    release_date_inferred = None if release_date is None else False

    return release_date, release_date_inferred
