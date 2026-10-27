// Numbered pages (the fish list): which page the URL asks for, how many
// there are, and which numbers to show.
//
// Pinned here:
//  * a page from the URL is a whole number from 1, else the first page;
//  * the links always reach the first and the last page and the ones beside
//    the current one, with a gap for any run skipped;
//  * "51–100 of 3,314": the range shown, which a short last page ends early.
import { describe, expect, it } from "vitest";
import { pageCount, pageLinks, pageRange, parsePage } from "./pages";

describe("parsePage", () => {
  it.each([
    [undefined, 1],
    ["1", 1],
    ["7", 7],
    ["0", 1],
    ["-2", 1],
    ["2.5", 1],
    ["seven", 1],
    ["7abc", 1],
    ["", 1],
  ])("reads %j as page %i", (raw, page) => {
    expect(parsePage(raw)).toBe(page);
  });
});

describe("pageCount", () => {
  it.each([
    [0, 50, 1],
    [1, 50, 1],
    [50, 50, 1],
    [51, 50, 2],
    [3314, 50, 67],
  ])("%i fish at %i a page is %i pages", (total, perPage, pages) => {
    expect(pageCount(total, perPage)).toBe(pages);
  });
});

describe("pageLinks", () => {
  it("lists every page when there are few", () => {
    expect(pageLinks(1, 1)).toEqual([1]);
    expect(pageLinks(3, 5)).toEqual([1, 2, 3, 4, 5]);
  });

  it("keeps the ends and the neighbors, with gaps between", () => {
    expect(pageLinks(1, 67)).toEqual([1, 2, "gap", 67]);
    expect(pageLinks(30, 67)).toEqual([1, "gap", 29, 30, 31, "gap", 67]);
    expect(pageLinks(67, 67)).toEqual([1, "gap", 66, 67]);
  });

  it("does not hide a single page behind a gap", () => {
    expect(pageLinks(4, 67)).toEqual([1, 2, 3, 4, 5, "gap", 67]);
    expect(pageLinks(64, 67)).toEqual([1, "gap", 63, 64, 65, 66, 67]);
  });
});

describe("pageRange", () => {
  it("is the fish this page shows", () => {
    expect(pageRange(1, 50, 3314)).toEqual({ first: 1, last: 50 });
    expect(pageRange(2, 50, 3314)).toEqual({ first: 51, last: 100 });
    expect(pageRange(67, 50, 3314)).toEqual({ first: 3301, last: 3314 });
  });
});
