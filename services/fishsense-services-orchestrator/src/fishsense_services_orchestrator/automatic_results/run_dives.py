"""The old name of `run_missions` (Phase 2a renamed it, PLAN.md §9.26).

Runbooks and the validation session's commands say
``python -m fishsense_services_orchestrator.automatic_results.run_dives``;
they keep working.
"""

from fishsense_services_orchestrator.automatic_results.run_missions import main

if __name__ == "__main__":
    main()
