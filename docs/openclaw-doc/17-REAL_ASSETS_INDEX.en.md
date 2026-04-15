> [中文版](17-REAL_ASSETS_INDEX.md)

# 17 · Real Assets Index

This page now keeps one job only:

> record which public-facing assets are still intentionally used, and what
> rules maintainers should follow when refreshing them.

Positioning first:

- this is a maintainer appendix
- it is not a default user entry page
- normal users should start from `README`, `15`, and `18`

---

## 1. Primary Assets Still Used Publicly

If an asset is not in this list:

- treat it as maintainer-side output by default
- do not keep linking it from user entry pages
- do not assume it is a stable public asset

### Pages

- `23-PROFILE_CAPABILITY_BOUNDARIES.html`
- `23-PROFILE_CAPABILITY_BOUNDARIES.en.html`

### Videos

- onboarding doc flow (Chinese / English)
- capability tour (Chinese / English)
- ACL scenario (Chinese / English)

### Key screenshots

- onboarding installed / uninstalled screenshots
- dashboard visual memory screenshots
- ACL screenshots
- memory-palace skill/chat proof screenshots
- the single profile-boundary chat still embedded by page `23`
- the dashboard page screenshots still embedded by `16`, `GETTING_STARTED`, and `TECHNICAL_OVERVIEW`

### Mermaid fallback images

The static Mermaid fallback PNGs are also part of the public asset set, because
they are directly embedded by the public markdown pages.

Important maintenance rule:

- if a Mermaid node/order/text changes
- update the `.mmd`, the rendered `.png`, and the markdown reference together

---

## 2. What Is No Longer Part Of The Default Public Surface

Do not describe these as default user-facing public assets anymore:

- older `profile-matrix/*` inventory screenshots that are not directly embedded
- extra `dashboard-current/*` inventory screenshots beyond the page screenshots still linked from the public docs
- duplicate stills that were previously kept only as maintainer-side evidence stock

If they are not directly linked from a public page anymore:

- do not treat them as stable public assets
- do not keep promising them in public docs
- remove them from the public repository when they are no longer needed

---

## 3. Safety Rule for Public Assets

Before reusing or refreshing a screenshot/video:

- check for usernames
- check for absolute local paths
- check for private keys or private endpoints
- check for host-specific internal state that should not become a public promise

When in doubt:

- keep the asset out of the public entry pages
- leave it as maintainer-only material instead
