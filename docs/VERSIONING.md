# Version status and release policy

Shed's published version labels do not currently form a monotonic Git history:

- the default branch package metadata and README identify the source tree as
  `0.2.0`;
- GitHub marks `v0.3.0` as the latest release; and
- the `v0.3.0` tag is an ancestor of the `v0.2.0` tag, with both tags behind
  the default branch.

These are historical facts, not a sequence to rewrite. The existing tags and
releases remain unchanged, and package metadata is not bumped merely to make
the public labels appear linear.

## Next release gate

No next version identifier has been assigned. Before another release, the
maintainer must record which current default-branch milestone establishes the
prospective release line in
[#41](https://github.com/CasterlyGit/shed/issues/41). A candidate is eligible
only when all of these are true:

1. its tag will point to the intended default-branch release commit;
2. package metadata, README status, and release notes agree on the new version;
3. supported Python tests, Ruff, CI, and the public-release guard pass;
4. setup effects, local and human-review boundaries, and benchmark scope have
   been rechecked against the candidate; and
5. release notes explain the non-linear historical tags, user-visible changes,
   verification performed, known limitations, and any data migration or local
   recovery action.

Shed is a local tool, not a hosted service. Its operational evidence is local
data review/reset, hook failure behavior, and repository rollback; it does not
need a public deployment or production-service rollback story to qualify for a
source release.
