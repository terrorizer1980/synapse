# -*- coding: utf-8 -*-
# Copyright 2015 OpenMarket Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from twisted.internet import defer

from synapse.http.servlet import (
    RestServlet, parse_string, parse_integer
)
from synapse.handlers.sync import SyncConfig
from synapse.types import StreamToken
from synapse.events.utils import (
    serialize_event, format_event_for_client_v2_without_event_id,
)
from synapse.api.filtering import Filter
from ._base import client_v2_pattern

import copy
import logging

logger = logging.getLogger(__name__)


class SyncRestServlet(RestServlet):
    """

    GET parameters::
        timeout(int): How long to wait for new events in milliseconds.
        since(batch_token): Batch token when asking for incremental deltas.
        set_presence(str): What state the device presence should be set to.
            default is "online".
        filter(filter_id): A filter to apply to the events returned.

    Response JSON::
        {
          "next_batch": // batch token for the next /sync
          "presence": // presence data for the user.
               "invited": [], // Ids of invited rooms being updated.
               "joined": [], // Ids of joined rooms being updated.
               "archived": [] // Ids of archived rooms being updated.
            }
          }
          "rooms": {
            "joined": { // Joined rooms being updated.
              "${room_id}": { // Id of the room being updated
                "event_map": // Map of EventID -> event JSON.
                "timeline": { // The recent events in the room if gap is "true"
                  "limited": // Was the per-room event limit exceeded?
                             // otherwise the next events in the room.
                  "events": [] // list of EventIDs in the "event_map".
                  "prev_batch": // back token for getting previous events.
                }
                "state": {"events": []} // list of EventIDs updating the
                                        // current state to be what it should
                                        // be at the end of the batch.
                "ephemeral": {"events": []} // list of event objects
              }
            },
            "invited": {}, // Ids of invited rooms being updated.
            "archived": {} // Ids of archived rooms being updated.
          }
        }
    """

    PATTERN = client_v2_pattern("/sync$")
    ALLOWED_PRESENCE = set(["online", "offline"])

    def __init__(self, hs):
        super(SyncRestServlet, self).__init__()
        self.auth = hs.get_auth()
        self.sync_handler = hs.get_handlers().sync_handler
        self.clock = hs.get_clock()
        self.filtering = hs.get_filtering()

    @defer.inlineCallbacks
    def on_GET(self, request):
        user, token_id = yield self.auth.get_user_by_req(request)

        timeout = parse_integer(request, "timeout", default=0)
        since = parse_string(request, "since")
        set_presence = parse_string(
            request, "set_presence", default="online",
            allowed_values=self.ALLOWED_PRESENCE
        )
        filter_id = parse_string(request, "filter", default=None)

        logger.info(
            "/sync: user=%r, timeout=%r, since=%r,"
            " set_presence=%r, filter_id=%r" % (
                user, timeout, since, set_presence, filter_id
            )
        )

        try:
            filter = yield self.filtering.get_user_filter(
                user.localpart, filter_id
            )
        except:
            filter = Filter({})

        sync_config = SyncConfig(
            user=user,
            filter=filter,
        )

        if since is not None:
            since_token = StreamToken.from_string(since)
        else:
            since_token = None

        sync_result = yield self.sync_handler.wait_for_sync_for_user(
            sync_config, since_token=since_token, timeout=timeout
        )

        time_now = self.clock.time_msec()

        rooms = self.encode_rooms(
            sync_result.rooms, filter, time_now, token_id
        )

        response_content = {
            "presence": self.encode_presence(
                sync_result.presence, filter, time_now
            ),
            "rooms": rooms,
            "next_batch": sync_result.next_batch.to_string(),
        }

        defer.returnValue((200, response_content))

    def encode_presence(self, events, filter, time_now):
        formatted = []
        for event in events:
            event = copy.deepcopy(event)
            event['sender'] = event['content'].pop('user_id');
            formatted.append(event)
        return {"events": formatted}

    def encode_rooms(self, rooms, filter, time_now, token_id):
        joined = {}
        for room in rooms:
            joined[room.room_id] = self.encode_room(
                room, filter, time_now, token_id
            )

        return {
            "joined": joined,
            "invited": {},
            "archived": {},
        }

    @staticmethod
    def encode_room(room, filter, time_now, token_id):
        event_map = {}
        state_events = filter.filter_room_state(room.state)
        recent_events = filter.filter_room_events(room.timeline.events)
        state_event_ids = []
        recent_event_ids = []
        for event in state_events:
            # TODO(mjark): Respect formatting requirements in the filter.
            event_map[event.event_id] = serialize_event(
                event, time_now, token_id=token_id,
                event_format=format_event_for_client_v2_without_event_id,
            )
            state_event_ids.append(event.event_id)

        for event in recent_events:
            # TODO(mjark): Respect formatting requirements in the filter.
            event_map[event.event_id] = serialize_event(
                event, time_now, token_id=token_id,
                event_format=format_event_for_client_v2_without_event_id,
            )
            recent_event_ids.append(event.event_id)
        result = {
            "event_map": event_map,
            "timeline": {
                "events": recent_event_ids,
                "prev_batch": room.timeline.prev_batch.to_string(),
                "limited": room.timeline.limited,
            },
            "state": {"events": state_event_ids},
            "ephemeral": {"events": room.ephemeral},
        }
        return result


def register_servlets(hs, http_server):
    SyncRestServlet(hs).register(http_server)
