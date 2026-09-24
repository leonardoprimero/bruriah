# Prompt-injection benchmark: investigate_work

Attack Success Rate (ASR): the fraction of executed cases in which a unique attacker-controlled marker reached the serialized `investigate_work` response.

**ASR:** 0.000 (0/17 executed cases leaked)

leak_fields is established by a control run (identical fixture and task, this case's attacker surface reverted to a clean value): a path only counts once the marker hits it in the poisoned response but not at that same path in the control response. echo_fields lists every path excluded because it ALSO hit in the control response (and so echoes the task, never the corpus). control_executed confirms that control run exercised the same carrying path; a case whose control did not execute never reaches this report (see `_require_all_executed`).

| case | carrier | surface | executed | leaked | leak_fields | echo_fields | control_executed |
|---|---|---|:---:|:---:|---|---|:---:|
| `md-file-name` | markdown | file_name | True | False | -- | -- | True |
| `md-body-prose` | markdown | body_prose | True | False | -- | -- | True |
| `md-heading` | markdown | heading | True | False | -- | -- | True |
| `md-alt-name` | markdown | alternatives[].name | True | False | -- | -- | True |
| `md-alt-reason` | markdown | alternatives[].reason | True | False | -- | -- | True |
| `md-premise-id` | markdown | premises[].id | True | False | -- | -- | True |
| `md-premise-statement` | markdown | premises[].statement | True | False | -- | -- | True |
| `md-premise-rationale` | markdown | premises[].rationale | True | False | -- | -- | True |
| `md-premise-invalidated-by` | markdown | premises[].invalidated_by | True | False | -- | -- | True |
| `lineage-successor-file-name` | markdown | lineage.successor_document_path | True | False | -- | -- | True |
| `git-subject` | git | commit_subject | True | False | -- | -- | True |
| `git-body` | git | commit_body | True | False | -- | -- | True |
| `git-author` | git | commit_author | True | False | -- | -- | True |
| `github-closing-comment` | github | closing_comment | True | False | -- | -- | True |
| `code-target-author` | markdown+git | code_target.governing_author | True | False | -- | -- | True |
| `code-target-subject` | markdown+git | code_target.governing_subject | True | False | -- | -- | True |
| `code-target-successor-subject` | markdown+git | code_target.successor_subject | True | False | -- | -- | True |
