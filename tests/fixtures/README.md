# Test fixtures

All files here are **synthetic**: hand-built rows in the flights.csv column layout, with
fictional tails (`N###ZZ`) and flight numbers. They are not real 2015 flights, even though the
airports are real codes present in `data/challenge/airport_capacity.csv` so the workbook limits
(turnaround, capacity, spare gates) apply as they would for real data.

| File | Date | Purpose |
|---|---|---|
| `rotation_day.csv` | 2015-03-10 | FABLE_ADDENDUM 10: one tail (`N900ZZ`) over four legs plus one connection (tests 15-20) |
| `cluster_day.csv` | 2015-03-10 | Cluster rules: shared runway window, unrelated airports (test 20) |
| `multi_trigger_day.csv` | 2015-03-12 | Scenarios E and F (HANDOVER 12) and the back-test (test 24), see below |

## multi_trigger_day.csv

Five qualifying DL triggers land at ATL within 2 h 05 min (earliest landings 12:30 to 14:35):

| Trigger | Route | Sched arr | Delay (cause) | Rotation after ATL | Next-leg LATE_AIRCRAFT |
|---|---|---|---|---|---|
| DL201 `N201ZZ` | DCA-ATL | 12:00 | 40 (late aircraft) | DL211 ATL-MCO 13:00, then DL221 MCO-ATL 15:15 | 25, then 5 |
| DL202 `N202ZZ` | BOS-ATL | 13:00 | 25 (weather) | DL212 ATL-DFW 14:00 | 10 |
| DL203 `N203ZZ` | MCO-ATL | 12:40 | 30 (late aircraft) | DL213 ATL-ORD 13:40, then DL223 ORD-ATL 16:00 | 20, then 0 |
| DL204 `N204ZZ` | DFW-ATL | 13:40 | 70 (50 late + 20 weather) | DL214 ATL-BOS 14:30 | 30 |
| DL205 `N205ZZ` | ORD-ATL | 14:20 | 25 (late aircraft) | DL215 ATL-DCA 15:20 | 0 |

Connections (same airline, other tail, 35-120 min after scheduled arrival at ATL): DL301
ATL-LGA 12:50 and DL302 ATL-MIA 15:00 are pure connection targets; the rotation legs above also
serve as connection targets for the other triggers.

Noise rows: DL401 CLT-ATL (on time, not a trigger), DL402 ATL-CLT (cancelled, skipped by the
graph) and DL404 ATL-SAV (blank tail, no rotation edge).

Worked example (DL201, input delay 40): slack to DL211 = 13:00 - 12:00 - 45 (ATL turnaround) = 15,
so DL211 carries 25; slack to DL221 = 15:15 - 14:30 - 35 (MCO) = 10, so DL221 carries 15;
`knock_on_min = 40`, `flights_affected = 2`. The back-test's actual knock-on for DL201 is 25 + 5 = 30.

Expected structure: DL201, DL202 and DL203 share one runway window (ETAs within 60 min);
DL204 and DL205 share another; the two groups are 80 min apart and not linked by rotation
or connection edges, so triage forms two clusters.
