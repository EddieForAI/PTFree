# SPDX-FileCopyrightText: 2026 EddieForAI
# SPDX-License-Identifier: Apache-2.0

"""Public site registry and per-adapter connection health for the shared UI.

Only implemented adapters belong here. The M-Team settings/storage schemas
remain compatible with 2.0.3; adding a new adapter also requires its own
settings and request/ownership implementation, not just a registry entry.
"""
import time

SITE_REGISTRY = (
    {'id': 'mteam', 'name': 'M-Team', 'favicon': 'https://static.m-team.cc/favicon.ico',
     'configured_field': 'site_added', 'credential_field': 'api_key_set',
     'freeseed_field': 'freeseed_mteam', 'traffic_field': 'mteam_enabled'},
)


def record_health(db, site_id, state):
    db.put('site_health_' + site_id, {'state': state, 'ts': time.time()})


def public_sites(config, traffic, db):
    sites = []
    for spec in SITE_REGISTRY:
        added = bool(config.get(spec['configured_field']))
        health = db.get('site_health_' + spec['id'], {}) or {}
        state = health.get('state', 'untested')
        stamp = health.get('ts')
        # Upgrade compatibility: 2.0.3 only stored M-Team Freeseed status.
        if spec['id'] == 'mteam' and not health:
            if db.get('auth_error', False):
                state = 'auth'
            elif db.get('last_search_ok'):
                state, stamp = 'ok', db.get('last_search_ok')
        if not config.get(spec['credential_field']):
            state = 'unconfigured'
        sites.append({**spec, 'configured': added, 'health': state, 'health_ts': stamp,
                      'freeseed_selected': bool(config.get(spec['freeseed_field'])),
                      'traffic_selected': bool(traffic.get(spec['traffic_field']))})
    return sites
