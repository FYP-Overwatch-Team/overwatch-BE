"""Knowledge graph pipeline: discover → extract → link → build.

Pure domain code. Nothing in this package imports FastAPI, Motor or the Neo4j
driver; databases and HTTP live in app/services and app/db, which depend on
this package and never the other way round. That keeps every stage testable
without containers and makes the pipeline reusable outside a request.
"""
