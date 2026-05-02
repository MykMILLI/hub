# HomeHub Copilot Instructions

## Modal Spacing Rule (Required)

- Every centered modal/overlay **must** include safe viewport padding so it never touches screen edges.
- Modal panels **must** keep enough surrounding space (desktop + mobile) and avoid cut-off headers/buttons.
- Modal content areas **must** support internal scrolling when content is tall.
- When creating or editing modals, prefer consistent centered styling already used in the app and preserve spacing with responsive padding.

## Enforcement

- Do not ship modal UI changes that can clip against viewport edges.
- If an existing modal is touched, update it to follow this rule before finishing.