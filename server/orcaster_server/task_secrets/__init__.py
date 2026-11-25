"""Task secrets per workspace and per worker.

- `keyring`: Fernet keys from `TASK_SECRETS_KEYS_PATH` and the fingerprint of the value.
- `service`: list, set, delete, resolve for the poll and audit the delivery.
- `gate`: worker version floor and SQL predicates for dispatching tasks
  that use stored secrets.
- `models`: documentary ORM mirror of the `task_secrets` table (migrations 0056 and 0059).
"""
