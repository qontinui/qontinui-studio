"""The launch kit (plan Phase 3).

A product's launch bundle is assembled from three inputs, in this order of
authority (D1: the record first, decoration second):

1. the factory's build record (``record``) — the only source of every number;
2. screenshots and a walkthrough of the running product (``capture``) — real
   pixels from the real product, never a generator (D6);
3. optional decorative assets from the Phase 2 pipeline (``decor``) — gated by
   D5/D6 there, labelled as AI-generated here, and never required.

``bundle`` assembles the static output, ``video`` the ~60 s film,
``publish`` runs the D7 gates over a bundle and publishes/unpublishes it, and
``trigger`` registers the coord ``unit_status`` gate that runs the kit when a
portfolio work unit ships.
"""
