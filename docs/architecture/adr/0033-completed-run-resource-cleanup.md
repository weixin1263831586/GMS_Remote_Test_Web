# ADR 0033: Resource cleanup after completed ATS runs

Status: Accepted

Completed runs release their device reservations through the existing Cluster
repository, including flash/AOA workflows that skip tests. Completion always
records a finish time. Cleanup receipts are persisted in the run result;
interruption or dependency failure keeps cleanup pending without repeating
flashing, AOA, tests or notifications.

The independent Worker maintenance loop retries pending and legacy completed
runs in bounded batches, ordered by update time without the history page cap.
A human creator or administrator may also request cleanup from the completed
run card. Agent/machine principals and other ordinary users remain forbidden
from this mutation surface (ADRs 0012 and 0032).

Cleanup validates reservation source identity and related command terminal
states. The Cluster transaction checks for active commands linked to that
reservation. Repeated releases finish the shared device-claim cleanup even
when a prior reservation transaction already committed; source-bound release
never detaches a claim later acquired by another task.
