/* Preserve the project entry response while entering the shared Atlas shell. */
(() => {
  'use strict';
  const target = new URL('/', location.origin);
  target.searchParams.set('atlas_projects', '1');
  for (const [key, value] of new URLSearchParams(location.search)) target.searchParams.append(key, value);
  location.replace(target.pathname + target.search);
})();
