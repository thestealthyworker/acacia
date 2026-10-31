# Data

- `airlines.csv`: complete (14 carriers).
- `sample_flights.csv`: 6 rows. Rows 1 to 5 are real rows from flights.csv (1 Jan 2015, all KNOCK_ON). Row 6, DL1639 DCA-ATL 26 Feb 2015, is **reconstructed** from the architecture's worked trace (tail UNKNOWN). Use it only until the full file is present.
- `sample_airports.csv`: 21 airports covering the sample routes and nearby diversion alternates.

Add the full files here:

- `flights.csv` (~38.6 MB subset of the 2015 US DOT on-time data, 31 columns). Do not commit it.
- `airports.csv` (322 airports; ECP, PBG and UST have blank coordinates).

Loader rule: if `flights.csv` exists use it, else fall back to `sample_flights.csv`; same for airports.
