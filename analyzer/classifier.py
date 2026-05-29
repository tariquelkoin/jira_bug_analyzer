
COMPONENT_AREA_MAP = {
    "Optimizer":          "Optimizer",
    "Query optimizer":    "Optimizer",
    "InnoDB":             "InnoDB",
    "Galera":             "Galera",
    "Replication":        "Replication",
    "DDL":                "DDL",
    "Partitioning":       "Partitioning",
    "Full-text search":   "Full-text Search",
    "JSON":               "JSON",
    "Stored routines":    "Stored Procedures / Triggers",
    "Triggers":           "Stored Procedures / Triggers",
    "Locking":            "Locking / Deadlock",
    "Crash recovery":     "Crash Recovery",
    "Backup":             "Backup",
    "Security":           "Security",
    "Performance schema": "Performance Schema",
    "MyISAM":             "MyISAM",
    "Aria":               "Aria",
    "RocksDB":            "RocksDB",
    "Spider":             "Spider",
    "CONNECT":            "CONNECT Engine",
    "Data types":         "Data Types",
    "Character sets":     "Character Sets / Collation",
    "Window functions":   "Window Functions",
    "CTE":                "CTE / Recursive Queries",
    "Sequences":          "Sequences",
}

KEYWORD_AREA_MAP = [
    (["wsrep", "galera", "sst", "ist"],                   "Galera"),
    (["innodb", "btr_cur", "row_ins", "dict_table"],       "InnoDB"),
    (["replication", "binlog", "slave", "relay log",
      "rpl_", "gtid"],                                     "Replication"),
    (["optimizer", "join_buffer", "range_check",
      "eq_ref", "derived", "subquery", "cost model"],      "Optimizer"),
    (["partition", "partitioning"],                        "Partitioning"),
    (["fulltext", "full-text", "ft_"],                     "Full-text Search"),
    (["json_", "json extract", "json_table"],              "JSON"),
    (["trigger", "stored procedure", "stored function",
      "sp_head", "sp_instr"],                              "Stored Procedures / Triggers"),
    (["deadlock", "lock wait", "lock_sys",
      "trx_lock", "waiting for lock"],                     "Locking / Deadlock"),
    (["crash recovery", "redo log", "ib_logfile",
      "doublewrite", "ibdata"],                            "Crash Recovery"),
    (["alter table", "create table", "drop table",
      "instant ddl", "online ddl"],                        "DDL"),
    (["window function", "over (", "rank()", "row_number()"], "Window Functions"),
    (["with recursive", "cte", "common table"],            "CTE / Recursive Queries"),
    (["create sequence", "nextval", "seq_"],               "Sequences"),
    (["myisam"],                                           "MyISAM"),
    (["aria"],                                             "Aria"),
    (["rocksdb"],                                          "RocksDB"),
    (["performance_schema", "performance schema"],         "Performance Schema"),
    (["backup", "mariabackup", "xtrabackup"],              "Backup"),
    (["ssl", "tls", "privilege", "grant", "auth_"],        "Security"),
    (["character set", "collation", "charset",
      "utf8", "latin1"],                                   "Character Sets / Collation"),
    (["decimal", "float", "double", "timestamp",
      "datetime", "geometry", "spatial"],                  "Data Types"),
]


def identify_area(summary, description, stack_trace, components, engines):
    """
    Rule-based area classification.
    Priority: JIRA components → keyword scan → storage engine fallback.
    Returns list of area strings (primary first).
    """
    areas = []

    # 1. JIRA components
    for comp in components:
        for comp_key, area in COMPONENT_AREA_MAP.items():
            if comp_key.lower() in comp.lower() and area not in areas:
                areas.append(area)

    # 2. Keyword scan over summary + stack trace + first 2000 chars of description
    scan = f"{summary}\n{stack_trace or ''}\n{(description or '')[:2000]}".lower()
    for keywords, area in KEYWORD_AREA_MAP:
        if area not in areas and any(kw in scan for kw in keywords):
            areas.append(area)

    # 3. Storage engine fallback
    engine_to_area = {
        "InnoDB": "InnoDB", "MyISAM": "MyISAM",
        "Aria": "Aria", "Galera": "Galera", "RocksDB": "RocksDB",
    }
    for engine in engines:
        area = engine_to_area.get(engine)
        if area and area not in areas:
            areas.append(area)

    return areas if areas else ["General"]
