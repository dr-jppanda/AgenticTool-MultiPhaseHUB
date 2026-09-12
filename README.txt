Adding the Analytics page to AgenticTool-MultiPhaseHUB — one new file only
============================================================================

Nothing in your repo is changed. build.py, dist/index.html, the catalog,
the pipeline (mhtdb/), and the tests are all untouched.

This adds exactly one new file:

  app/dist/analytics.html

What it is: the same usage/demographics dashboard (visits by country as a
bubble map, daily visits, top pages, what people do inside the AgenticTool)
that you already reviewed, now as a plain static HTML/JS/CSS page — same
style as your existing dist/index.html, no framework, no build step.

It pulls real data client-side from your live backend:
  https://backend-nx4f.onrender.com/api/analytics/summary/
using the browser's own cookies (credentials: 'include'), so it only shows
data to someone already signed in as staff on multiphasehub.org — anyone
else sees a plain "Staff sign-in required" message instead of charts, since
the backend itself enforces is_staff (not just this page).

Where it ends up live depends on how you deploy app/dist/ today:
  - If dist/ gets copied into the Next.js site's public/agentic-tool-app/
    folder (as your existing dashboard does, per that repo's page.jsx),
    analytics.html lands at public/agentic-tool-app/analytics.html and is
    reachable at https://www.multiphasehub.org/agentic-tool-app/analytics.html
  - If you serve app/dist/ some other way, it's just a sibling static file
    next to index.html, same as any other page in that folder.

It isn't linked from anywhere (not from index.html, not from the main
site's nav) — reachable only by that direct URL, matching "just add the
page, don't change any code."

To push:
  cd AgenticTool-MultiPhaseHUB
  # copy app/dist/analytics.html from this delivery into your repo at the same path
  git add app/dist/analytics.html
  git commit -m "Add staff-only usage analytics dashboard page"
  git push
