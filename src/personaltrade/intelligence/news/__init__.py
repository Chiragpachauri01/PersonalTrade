"""News ingestion (ROADMAP M13): fetch from registered `NewsProvider`s, dedup,
tag against the instrument universe, and persist — the raw material M14's
prompt builder later reads. News text is untrusted input end to end.
"""
