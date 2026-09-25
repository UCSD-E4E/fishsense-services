"""FishSense v2 processor: the compute side of the pipeline.

Ported module by module from v1's ``fishsense-data-processing-workflow-worker``
(PLAN.md §6.3). It speaks only the processing contract
(``fishsense_services_contracts``): no database, no API client. Each ported
module names the v1 commit it came from.
"""
