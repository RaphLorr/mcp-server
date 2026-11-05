# State validation

Story and Bug writes containing `status` or `v_status` are validated before
attachment handling and before the ticket update. Writes without state fields
keep the existing behavior. Task state handling uses the separate task API.

- Updates first read the exact ticket and its project. Story queries use the
  ticket's `workitem_type_id`; do not merge states from different categories.
- `get_workflows_status_map` defines writable business states and aliases.
  `get_workflows_last_steps` identifies configured terminal states, which can be
  different for each project and category and can include multiple outcomes.
- `get_workflows_all_transitions` defines direct edges for a state change.
  Boundary nodes such as `start` or `end` in an edge are not writable merely
  because the edge mentions them. A state named `end` remains valid if it really
  exists in the target workflow's state map and the transition allows it.
- The server resolves an exact, unique `v_status` to its canonical `status` and
  rejects conflicting simultaneous values. It never guesses a closing state,
  chooses rejection on the user's behalf, or executes intermediate transitions.
- Invalid targets, missing/ambiguous ticket data, unreadable workflow config and
  unsupported response shapes stop the write. Errors include `updated=false`,
  the query scope, legitimate states and configured terminal states when known.
  Provider exception text and credentials are not copied into those errors.
- Category changes and state changes must be separate when changing workflows.
  A ticket already carrying a corrupt state needs investigation and repair;
  the guard does not invent a transition from an unconfigured state.
- Creating a Story with an explicit state requires its category ID. Omitting
  state fields preserves TAPD's normal creation defaults.

These checks validate state membership and direct workflow edges. TAPD remains
responsible for permissions, transition-specific required fields and concurrent
changes after validation. This protection does not repair previously corrupted
tickets or change an agent's skill instructions.
