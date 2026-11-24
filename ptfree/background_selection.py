# SPDX-FileCopyrightText: 2026 EddieForAI
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from .background_store import GIB
from .policy import timestamp

PAGE_SIZE = 100


def site_window(config, now):
    start, end = timestamp(config['site_free_start']), timestamp(config['site_free_end'])
    return end if start and end and start <= now < end else None


def classify(item, config, now):
    """Return the lane and the applicable deadline for the current offer."""
    if not item['visible'] or item['banned'] or item['hr'] is True:
        return 'excluded', None
    personal = item.get('personal_end')
    own_end = item.get('intrinsic_end', item.get('free_end'))
    own = item.get('intrinsic_free', item['free']) and (own_end is None or own_end > now)
    event_end = site_window(config, now)
    # An independently free offer can be displayed with the event deadline.
    # When the deadline coincides, validate the item again at the end of the event.
    event_overlay = bool(event_end and own_end and abs(event_end - own_end) <= 120)
    if personal and personal > now:
        return 'freeseed', personal
    if not own and not event_end:
        return 'excluded', None
    if own_end and own and not event_overlay:
        return 'freeseed', own_end
    created = item.get('created')
    if created is None or created > now + 300:
        return 'excluded', None
    deadline = event_end if event_end and (not own or own_end is None or event_overlay) else own_end
    return ('freeseed' if now - created < 86400 else 'background'), deadline


def size_reason(size, config):
    if not size:
        return '种子大小未知'
    if size < config['min_size_gib'] * GIB:
        return '体积低于下限'
    if config['max_size_gib'] and size > config['max_size_gib'] * GIB:
        return '体积超过上限'
    limit = config['ram_capacity_gib'] if config['storage'] == 'ram' else config['capacity_gib']
    if size > limit * GIB:
        return '超过当前存储配额'
    return ''


def search_page(site, config, page, discount):
    modes = [name for name in ('normal', 'adult') if config[name]]
    categories = config['categories'].replace('，', ',')
    categories = [int(value.strip()) for value in categories.split(',') if value.strip()]
    for mode in modes:
        values = site.search(mode=mode, page=page, sort='SEEDERS', page_size=PAGE_SIZE,
                             discount=discount,
                             categories=categories if mode == 'normal' else ())
        yield mode, values[:PAGE_SIZE]
