NAMESPACE_DEFAULT = "agent:hermes"
PROVIDER_ID_DEFAULT = "mnemosyne-rust"
PREFETCH_TOOL = "mnemosyne_prefetch"
SYNC_TURN_TOOL = "mnemosyne_sync_turn"
CHECKPOINT_API_VERSION = 2


class ProviderConfig:
    def __init__(self, binary="mnemosyne", namespace=NAMESPACE_DEFAULT, db_path=None,
                 hermes_home=None, policy_owner=None, provider_id=PROVIDER_ID_DEFAULT,
                 request_timeout=10, initialize_timeout=15, prefetch_timeout=3,
                 shutdown_timeout=5, eager_connect=False):
        self.binary = binary
        self.namespace = namespace
        self.db_path = db_path
        self.hermes_home = hermes_home
        self.policy_owner = policy_owner if policy_owner is not None else provider_id
        self.provider_id = provider_id
        self.request_timeout = request_timeout
        self.initialize_timeout = initialize_timeout
        self.prefetch_timeout = prefetch_timeout
        self.shutdown_timeout = shutdown_timeout
        self.eager_connect = eager_connect

    def resolved_db_path(self):
        if self.db_path:
            return self.db_path
        if self.hermes_home:
            return f"{self.hermes_home}/mnemosyne/mnemosyne.db"
        import os
        home = os.environ.get("MNEMOSYNE_HERMES_HOME", os.path.expanduser("~/.hermes"))
        return f"{home}/mnemosyne/mnemosyne.db"

    def storage_dir(self):
        import os
        db_path = self.resolved_db_path()
        dir_path = os.path.dirname(db_path)
        if dir_path:
            return dir_path
        return "."

    def resolved_namespace(self):
        return self.namespace


def default_config():
    import os
    binary = os.environ.get("MNEMOSYNE_BIN", "mnemosyne")
    namespace = os.environ.get("MNEMOSYNE_NAMESPACE", NAMESPACE_DEFAULT)
    db_path = os.environ.get("MNEMOSYNE_DB_PATH", None)
    hermes_home = os.environ.get("MNEMOSYNE_HERMES_HOME", None)
    policy_owner = os.environ.get("MNEMOSYNE_POLICY_OWNER", None)
    provider_id = os.environ.get("MNEMOSYNE_PROVIDER_ID", PROVIDER_ID_DEFAULT)
    request_timeout = int(os.environ.get("MNEMOSYNE_REQUEST_TIMEOUT", "10"))
    initialize_timeout = int(os.environ.get("MNEMOSYNE_INITIALIZE_TIMEOUT", "15"))
    prefetch_timeout = int(os.environ.get("MNEMOSYNE_PREFETCH_TIMEOUT", "3"))
    shutdown_timeout = int(os.environ.get("MNEMOSYNE_SHUTDOWN_TIMEOUT", "5"))
    eager_connect = os.environ.get("MNEMOSYNE_EAGER_CONNECT", "off").lower() in ("1", "on", "true")
    return ProviderConfig(
        binary=binary,
        namespace=namespace,
        db_path=db_path,
        hermes_home=hermes_home,
        policy_owner=policy_owner,
        provider_id=provider_id,
        request_timeout=request_timeout,
        initialize_timeout=initialize_timeout,
        prefetch_timeout=prefetch_timeout,
        shutdown_timeout=shutdown_timeout,
        eager_connect=eager_connect,
    )
