/**
 * Numbered pages, for the fish list (`?page=`): which page the URL asks for,
 * how many there are, and which numbers the pager shows.
 */

/** A page from the URL: a whole number from 1; anything else is the first. */
export function parsePage(raw: string | undefined): number {
  if (!raw || !/^\d+$/.test(raw)) return 1;
  const page = Number(raw);
  return page >= 1 ? page : 1;
}

/** How many pages `total` makes, `perPage` to a page: always at least one. */
export function pageCount(total: number, perPage: number): number {
  return Math.max(1, Math.ceil(total / perPage));
}

/**
 * The page numbers to link: the first, the last, and the ones beside the
 * current one, with a `"gap"` for each run skipped. A gap never stands for a
 * single page, since the number takes no more room than the gap.
 */
export function pageLinks(current: number, last: number): (number | "gap")[] {
  const shown = new Set([1, last, current - 1, current, current + 1]);
  const pages = [...shown].filter((n) => n >= 1 && n <= last).sort((a, b) => a - b);
  const links: (number | "gap")[] = [];
  for (const n of pages) {
    const previous = links.at(-1);
    if (typeof previous === "number" && n - previous === 2) links.push(n - 1);
    else if (typeof previous === "number" && n - previous > 2) links.push("gap");
    links.push(n);
  }
  return links;
}

/** The fish a page shows, counted from 1: "51–100 of 3,314". */
export function pageRange(page: number, perPage: number, total: number): { first: number; last: number } {
  const first = (page - 1) * perPage + 1;
  return { first, last: Math.min(total, page * perPage) };
}
