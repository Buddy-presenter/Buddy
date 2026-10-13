# PostgreSQL persistence

Issue #3 adds durable users, decks, processing jobs, analysis versions and presentation
sessions through PostgreSQL, SQLAlchemy and Alembic. Repository reads require owner IDs;
composite foreign keys enforce that child records use the same owner and deck. These are
internal persistence boundaries. Public workflows/authentication remain in their issues.

Docker is optional: use an existing PostgreSQL 18 server, a native installation, or a
local container. Database credentials come from environment configuration. Migrations
run explicitly before API startup; the API never creates or upgrades tables itself.

Implementation and real PostgreSQL validation are in progress on the draft PR.
