"""Temporal task queues the processor serves.

A queue name is an agreement *between* the orchestrator (which dispatches a
child workflow to it) and the processor (which polls it), so it lives here,
owned by neither. A typo on either side is silent: the child is accepted and
sits `Running` until its execution timeout (v1's task_queues.py).

The split is v1's, for v1's reasons (fishsense-lite@a8b2c3bc
libs/fishsense-shared/src/fishsense_shared/task_queues.py):

* **the per-image queue** decodes full-res `.ORF`s (1-3 GB each), so its
  concurrency is capped by memory;
* **the light queue** holds no image bytes -- rows in, numpy, rows out -- and
  exists so sub-second work is never stuck behind that cap;
* **the GPU queue** runs model inference, served by a GPU deployment and a CPU
  fallback under the same name.

The names are v2's own, never v1's: Temporal is shared until cutover, and a v1
data-worker would take a v2 child from a v1 queue.
"""

PROCESSOR_TASK_QUEUE = "fishsense_processor"
PROCESSOR_LIGHT_TASK_QUEUE = "fishsense_processor_light"
PROCESSOR_GPU_TASK_QUEUE = "fishsense_processor_gpu"
