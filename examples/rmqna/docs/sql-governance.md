# ADR: rmqna runs model-written SQL

Status: accepted, 19 October 2026. Applies to `mcp-servers/rmqna-data` only. The library is unchanged.

## Context

The library governs data through **named queries**. The `DataSource` port says a caller "never
supplies query text, so nothing a model writes can become part of a statement"
(`packages/agentcraft2-core/src/agentcraft2_core/contracts/data.py`). Every query is declared in a
file with its parameters, row limit and classification, and each call passes a `data.query` policy
decision that can mask columns and cap rows. rmprep keeps to this: its four tools compose reviewed
queries.

rmqna exists to answer questions no fixed catalog anticipates ("which clients' utilization rose last
quarter?", "weighted pipeline by product?"). Its agent writes the SQL, the server runs it, and the
agent answers from the rows. That is a deliberate departure from the named-query stance, so it is
recorded here rather than left implicit.

## Decision

1. **The departure is local to this example.** The `DataSource` port, the `data.query` action and the
   `DataSourceContract` suite are untouched. rmqna does not use them: its server holds its own engine
   (`engine.py`) behind its own validator (`validate.py`), and offers three tools.
2. **Containment moves from "no query text" to "validated query text"**, with the controls below. The
   server is the boundary. The agent's prompt asks for read-only SQL, but nothing depends on the
   model complying.
3. **rmqna is local-only.** The engine is an in-memory DuckDB over CSV files; it loads the workspace's
   `data/` folder by a module constant (`settings.DATA_DIR`), not by a setting, and serves the schema
   document from the agent's package. There is no deployable form of this server.
4. **No new tracing or logging machinery.** The SQL is visible where the model's tool call already is
   (the agent's tool span in Phoenix and the model trace); the server's own log never carries it.

## Compensating controls, as built

Each control names where it lives and the test that pins it. Tests are in
`mcp-servers/rmqna-data/tests/`.

| # | Control | Where | Pinned by |
| --- | --- | --- | --- |
| 1 | **One statement, and it is a plain SELECT.** Deny by default: INSERT, UPDATE, DELETE, MERGE, CREATE, DROP, ALTER, COPY, PRAGMA, SET, ATTACH, DETACH, INSTALL, LOAD, CALL, USE, DESCRIBE, SHOW, SUMMARIZE, EXPLAIN, ANALYZE, a standalone VALUES, `SELECT ... INTO`, and any of these nested inside a SELECT are `not_a_select`; so are PIVOT and UNPIVOT in every form, statement or operator, nested or not (DuckDB expands a dynamic pivot into a `CREATE TYPE` plus a SELECT); a second statement is `multiple_statements`. | `validate._single_select`, `_refuse_nested_statements` | `test_each_write_and_ddl_statement_kind_is_refused`, `test_pragma_attach_copy_install_load_set_are_refused`, `test_two_statements_are_refused`, `test_pivot_and_unpivot_are_refused_in_every_form`, `test_a_refused_statement_is_named_for_what_it_is` |
| 2 | **Validation at the syntax tree, never the text.** The text is parsed with sqlglot in the DuckDB dialect; what runs is the statement rendered back from the tree, with comments dropped. A keyword in a string or a comment is data, and a second statement hidden behind a comment cannot reach the engine. Validation is synchronous and runs on the event loop before the timeout starts, so it must be cheap: the text is capped at 20,000 characters and nothing in it may backtrack (naming a refused statement once used a regular expression that took 19 seconds on 28 nested comments; it now reads the first token with sqlglot's tokenizer). | `validate_sql` | `test_a_second_statement_hidden_behind_a_comment...`, `test_text_after_a_line_comment_marker...`, `test_a_hostile_string_value_is_just_a_value`, `test_a_backslash_in_a_string_cannot_close_the_literal_early`, `test_the_original_text_is_never_returned_unparsed`, `test_naming_a_refused_statement_cannot_be_made_to_take_forever` |
| 3 | **Table allowlist.** Every table must be one of the loaded tables, named bare. A name defined by `WITH` is resolved by scope: it is not an unknown table where the CTE is visible, and is one where it is not. Schema-qualified names, the system catalog and quoted file names are `unknown_table`. Names compare ASCII-only, so a Unicode look-alike is not a table. | `validate._refuse_unknown_tables`, `_cte_references` | `test_an_unknown_table_is_refused_naming_it`, `test_a_cte_alias_is_not_a_table_where_the_cte_cannot_be_seen`, `test_a_qualified_or_catalog_or_file_name_is_not_a_table`, `test_a_look_alike_of_a_table_name_is_not_that_table` |
| 4 | **Function denylist, and no function as a table.** `read_*`, `duckdb_*`, `pragma_*`, `parquet_*`, `*_scan`, `getenv`, `glob`, `current_setting`, `query`, `query_table` and a short list more are `forbidden_function` wherever they appear. Any function in a FROM position is refused whatever its name. Functions that make rows, a list or a string of any size out of a number, or a wide string out of a separator (`range`, `generate_series`, `generate_subscripts`, `unnest`, `repeat`, `list_resize`, `array_resize`, `rpad`, `lpad`, `printf`, `format`, `equi_width_bins`, `concat_ws`) are refused **anywhere** in the statement: nothing over these tables needs one, and DuckDB's memory limit does not stop them (measured: `repeat('a', 3e9)` and `unnest(range(0, 3e9))` pass 5 GB under a 1 GB limit; `rpad(name, 20000000, 'y')` over a join took 8 GB and ran 25 seconds past its timeout; `equi_width_bins(0.0, amount, 1000000, false)` per row took 11 GB and ran 15 seconds past it; `concat_ws` with a 9000-character separator and 2000 arguments took 15.1 GB, inside the statement length cap). They are named, not found by a blanket bound on integer arguments, which would refuse ordinary calls such as `to_timestamp(1750000000)`. This is a list of the ones measured, not a proof that no other function (`replace` on a huge string, and its kind) can allocate. Everyday functions are not caught. | `validate._refuse_functions` | `test_table_functions_are_refused`, `test_a_generator_of_rows_or_memory_is_refused_anywhere_in_the_statement`, `test_ordinary_functions_are_not_in_the_way` |
| 5 | **No set operations**, anywhere in the statement (`set_operation`). | `validate._refuse_set_operations` | `test_a_set_operation_is_refused_naming_it` |
| 6 | **Structural caps.** At most 16 sources (tables and subqueries) joined in one SELECT, at most 32 joins in a whole statement however they are grouped (sqlglot hangs the joins of a parenthesized group on the group's table, so a per-SELECT count alone missed `FROM (a JOIN b ...), (c JOIN d ...)`), and at most 32 references to CTEs plus subqueries in one statement, refused as `parse_error` before DuckDB is asked. DuckDB's binder and optimizer do not stop when interrupted, so a statement that is expensive to *plan* (review observed a 26-level chain of CTEs that each read the previous one twice, and a 400-way self-join, still running at 20 seconds and over 5 GB after a 1 second timeout) outlives any timeout; these caps keep ordinary SQL far under the limit and make the pathological shapes unreachable by accident. They do not bound a deliberate squaring chain's *execution*, which the timeout handles. | `validate._refuse_too_many_joins`, `_refuse_too_tangled` | `test_a_doubling_cte_chain_is_refused_by_the_structural_cap`, `test_a_self_join_of_hundreds_of_sources_is_refused_before_duckdb_sees_it`, `test_parenthesized_join_groups_do_not_hide_how_many_joins_there_are`, `test_the_runaways_the_timeout_cannot_stop_never_reach_duckdb` |
| 7 | **Row cap of 200 and a byte ceiling of 2 MB.** The row cap is set at the syntax tree. The outermost LIMIT is set to 201, or lowered to it; a smaller LIMIT and the OFFSET are kept. The engine fetches at most 201 rows, returns 200, and `truncated` says whether the 201st existed. A LIMIT that is not a plain row count (a percentage, WITH TIES, an expression) is refused rather than guessed at. The row cap does not bound a result's size (one row can hold a whole table as a list: `SELECT list(t) FROM transactions t` is one row of 7 MB), so the converted result is also held to a serialized-size ceiling of 2 MB, counted as compact JSON with non-ASCII and control characters escaped (a control character is six bytes, so counting characters would let 1.9 million of them through a ceiling that then weighs 11 MB), as the rows are converted and abandoned the moment it is passed; over it the call returns `execution_error` asking for a narrower selection. The cap is rows and bytes. The conversion runs in the worker thread, inside the timeout, not on the event loop. | `validate._cap_rows`, `engine.execute` | `test_a_large_limit_is_lowered_and_a_small_one_kept`, `test_an_offset_survives_the_limit_rewrite`, `test_truncation_is_flagged_at_the_cap`, `test_a_limit_that_is_not_a_plain_row_count_is_refused_not_guessed`, `test_a_result_that_is_one_huge_row_is_refused_by_size_not_returned`, `test_the_byte_ceiling_is_the_engines_to_set`, `test_the_byte_ceiling_counts_what_the_json_weighs_not_characters`, `test_what_the_budget_spends_is_never_less_than_what_the_json_weighs`, `test_rows_are_converted_in_the_worker_thread_not_on_the_event_loop` |
| 8 | **Timeout of 30 seconds, with interrupt, and an honest count of what is still running.** Each call runs in a worker thread under `asyncio.wait_for`; on timeout or cancellation the cursor is interrupted, which stops DuckDB's *executor* (joins, scans, aggregations) within moments. It does not stop everything: the binder, the optimizer and one huge scalar evaluation run to completion, so **a pathological statement can outlive its timeout, and its worker thread keeps running (and holding memory) after the caller was told `timeout`**. `RmqnaEngine.running` counts those workers. The structural caps (6) and the generator refusals (4) are what make that hard to reach; they are not a guarantee. Stopping it for certain needs process isolation (graduation path). | `engine._run`, `RmqnaEngine.running` | `test_a_timeout_interrupts_the_query` and `test_a_cancelled_call_interrupts_the_query` (the worker really finishes soon after), `test_the_interrupt_tests_can_fail_when_nothing_is_interrupted` |
| 9 | **External access disabled, configuration locked.** After the tables load, `enable_external_access` is false and the configuration is locked, so no statement can read a file or the network, install an extension or turn this back on. **Spilling to disk is disabled too** (`temp_directory` is empty): DuckDB otherwise writes what does not fit in memory to a `.tmp` folder in the working directory, bounded only by free disk space (a window function over a cross join wrote 3.6 GB in 8 seconds in review), so "touches no file" would not have been true. A statement that does not fit in memory now fails at the memory limit with a clean `Out of Memory Error`, and the engine writes no file. (The directory is set before external access is turned off, which forbids changing it.) DuckDB is also given a **memory limit of 1 GB** (and the session time zone is set to UTC, so an answer does not depend on the machine). The limit is a backstop: DuckDB does not account for every allocation, and it does not stop what is listed under control 4. | `engine._load` | `test_external_access_is_disabled`, `test_the_configuration_cannot_be_changed_after_loading`, `test_the_memory_limit_is_set_and_locked`, `test_nothing_is_written_to_disk_when_a_statement_does_not_fit_in_memory`, `test_the_temp_directory_is_empty_and_locked`, `test_a_statement_that_runs_out_of_memory_comes_back_as_an_error` |
| 10 | **A second parser.** Before running, the engine asks DuckDB itself (nothing is executed) to parse the rewritten text and insists on exactly one statement of kind SELECT; it then runs the statement DuckDB parsed. If sqlglot and DuckDB ever read one text differently, the disagreement stops here. | `engine._the_one_select` | `test_the_engine_asks_duckdb_itself_before_running_what_the_validator_passed` |
| 11 | **Only content is queryable.** `data_model.csv` (the dictionary) and `rm_scenarios.csv` (rmprep's answer key) are never loaded, wherever they sit. The tables are an in-memory copy, so the worst a bypass could damage is that copy. | `engine._load`, `NOT_QUERYABLE` | `test_the_dictionary_and_an_answer_key_are_never_loaded_even_when_present`, `test_tables_are_the_31_content_csvs` |
| 12 | **Value lookup takes no SQL.** `search_column_values` checks the table and the column against the loaded schema (an allowlist, not quoting) and binds the search text as a parameter; `%` and `_` in it are text. | `engine.values` | `test_values_takes_identifiers_from_the_loaded_schema_only`, `test_values_binds_the_search_as_a_parameter`, `test_values_treats_wildcards_as_text` |
| 13 | **Tool-level governance, unchanged library machinery.** Identity, the registry, `tool.call` policy for `analyst` and `manager` (everyone else denied by default), guardrails and the audit record. The tools are listed in the registry and every one is pinned. | library; `policies/agentcraft2/rules/data.yaml`, `registry/mcp-tools.yaml` | `test_any_other_role_is_denied_before_the_tool_runs`, `test_every_tool_is_pinned_and_the_pins_are_current`, `tests/policy-samples.yaml` |
| 14 | **Content-free logs.** The server writes one line per call: tool, duration, row and column counts, truncated flag and error kind. Never the SQL, a search text or a value. sqlglot logs warnings that quote the statement (falling back to a raw command, a scope it cannot traverse); every record of the `sqlglot` logger is dropped for the duration of each validation, by a filter that exists only for that synchronous call. The audit record holds the decision, not the SQL. | `server._log_call`, `validate._sqlglot_silenced` | `test_the_server_logs_hold_counts_and_error_kinds_and_never_the_sql`, `test_validation_writes_nothing_to_the_log`, `test_sqlglots_scope_warning_is_silenced_without_leaning_on_the_pivot_refusal`, `test_the_audit_log_holds_the_decision_and_not_the_sql` |
| 15 | **Refusals are results the agent can repair from.** A refusal or a DuckDB error is a normal tool result, `{error_kind, message}`, with DuckDB's text verbatim for engine errors. The message says what to change. | `server`, `validate.SqlRefused` | `test_a_refusal_is_a_normal_result_the_agent_can_read`, `test_execution_errors_come_back_structured` |

The refusal vocabulary is `not_a_select`, `multiple_statements`, `set_operation`, `unknown_table`,
`forbidden_function`, `parse_error`, `execution_error` and `timeout`. The structural caps and an
over-long or over-nested statement are `parse_error` ("not accepted as written"); a result over the
byte ceiling is `execution_error`.

## Narrowings

What the named-query contract gives, and this server does not:

- **No per-query classification and no obligations.** The library decides `data.query` per named query
  and can mask columns and cap rows per role. Model-written SQL has no query name to decide on, so
  there is no such decision. An `analyst` and a `manager` see the same columns, including contact
  names, emails and phones, up to the row cap. The tool-level rule is a role gate and nothing finer.
  The `data.query` rules and samples of the generated sample tools were removed with them.
- **No per-role masks on ad-hoc results.** The data is synthetic and local. If it were not, this
  server would not be acceptable as built.
- **The schema document's silence about contact details is not a control.** It keeps those values out
  of the prompt; `execute_sql` and `search_column_values` can still read them.
- **Classification is per tool, not per table.** `execute_sql` and `search_column_values` are
  `confidential` in the registry; `get_schema` is `internal`.
- **The dialect is DuckDB.** The library's convention is to write Redshift and transpile. That is
  traded away here because nothing deploys.
- **The validator is narrower than DuckDB.** These are refused although DuckDB accepts them, each with
  a message naming what to do instead: set operations (so `UNION ALL` inside a CTE, and with it
  recursive CTEs), PIVOT and UNPIVOT in every form, `SUMMARIZE`, `range`, `generate_series`,
  `generate_subscripts`, `unnest`, `repeat`, `list_resize`, `array_resize`, `rpad`, `lpad`, `printf`,
  `format`, `equi_width_bins` and `concat_ws` anywhere, any function as a table, schema-qualified
  names, a LIMIT that is not a plain row count, more than 16 joined sources in a SELECT, 32 joins or
  32 CTE and subquery references in a statement, anything over 20,000 characters, a result over
  2 MB, and syntax sqlglot cannot parse.
  Each refusal is one place in `validate.py` or `engine.py` and can be relaxed there, with a test,
  when a real question needs it.

## What this does not claim

- **A pathological statement can outlive its timeout.** Interrupting stops DuckDB's executor, not its
  binder, its optimizer or a single huge scalar evaluation. Statements like those ran for 20 seconds
  or more and took 5 to 12 GB after a 1 second timeout had fired, and their worker thread kept
  running after the caller was told `timeout`. The structural caps, the generator refusals and the
  memory limit make them hard to reach; none of the three is a guarantee (the memory limit in
  particular is not honoured for every allocation). `RmqnaEngine.running` shows what is still going.
  A runaway also holds one of the event loop's default executor threads. For a local, single-user
  example that is accepted; the way to stop one for certain is process isolation (below).
- **The two parsers are not proven equivalent.** Control 10 makes a disagreement harmless for
  statement kind and count, and the tests cover quotes, backslashes and comments. The remaining risk
  is a bug in DuckDB's own handling of a SELECT; the floor under it is controls 9 and 11: no file or
  network access, and only an in-memory copy to damage.
- **Results are data, not instructions.** Row values flow back to the model. The agent side frames
  every tool result as untrusted data (library behaviour); the server does no content filtering.
- **A model can read every loaded table in full**, 200 rows at a time. Row-level security is out of
  scope (see the narrowings).

## Graduation path

If free-form SQL ever moves into the library, it should be a new governed port, not a loosening of
`DataSource`:

1. A `SqlSource` port beside `DataSource`, with the validator as part of the adapter's contract and
   a contract test suite like `DataSourceContract`.
2. A **statement-level policy decision**: a new action (for example `sql.execute`) whose resources are
   the tables the validator found, so a rule can grant `accounts` to an analyst and not `contacts`.
   The classification of a result is the highest of the tables it read.
3. **Obligations enforced by rewriting the statement**: column masks and row caps applied at the
   syntax tree (a masking subquery in place of each table), not by post-processing rows.
4. **Audit of the statement** by hash, tables and outcome, so it can be reproduced without the text
   being in the log.
5. **A read-only database role** as the hard floor in any deployed environment, and a cost limit
   (for example from `EXPLAIN`) beside the timeout.
6. **Process isolation per statement** (or a pool of worker processes), with an address-space limit
   and a hard kill on timeout. It is the only way to bound planning, scalar evaluation and memory
   for certain; every in-process control above is a mitigation.
7. A **dialect-neutral validator** (sqlglot transpiling to the production dialect), so the library's
   Redshift convention holds again.

Until then this stays an example, with the controls above and the narrowings stated.
