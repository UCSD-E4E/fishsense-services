"use client";

// An admin creates a deployment: one field trip, named once per tenant
// (e4edm's dataset name, YYYY.MM.DD.PROJECT.LOCATION, where there is one).

import { useState, useTransition } from "react";
import { createDeploymentAction } from "../missions/actions";

const field =
  "rounded-md border border-slate-300 bg-white px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-900";

export function NewDeploymentForm({ slug }: { slug: string }) {
  const [form, setForm] = useState({ name: "", started_on: "", ended_on: "", notes: "" });
  const [pending, start] = useTransition();
  const [error, setError] = useState<string | null>(null);

  function set(key: keyof typeof form) {
    return (e: React.ChangeEvent<HTMLInputElement>) =>
      setForm((f) => ({ ...f, [key]: e.target.value }));
  }

  function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    start(async () => {
      const result = await createDeploymentAction(slug, form);
      if (result.ok) setForm({ name: "", started_on: "", ended_on: "", notes: "" });
      else setError(result.error);
    });
  }

  return (
    <form
      onSubmit={submit}
      className="space-y-3 rounded-lg border border-slate-200 p-5 dark:border-slate-800"
    >
      <h2 className="text-lg font-semibold">New deployment</h2>
      <div className="flex flex-wrap gap-2">
        <input
          aria-label="Name"
          required
          className={`${field} min-w-72 flex-1`}
          placeholder="2026.03.02.FishSense.Curacao"
          value={form.name}
          onChange={set("name")}
        />
        <input aria-label="Started on" type="date" className={field} value={form.started_on} onChange={set("started_on")} />
        <input aria-label="Ended on" type="date" className={field} value={form.ended_on} onChange={set("ended_on")} />
      </div>
      <input
        aria-label="Notes"
        className={`${field} w-full`}
        placeholder="Notes"
        value={form.notes}
        onChange={set("notes")}
      />
      <div className="flex items-center gap-3">
        <button
          type="submit"
          disabled={pending}
          className="rounded-md bg-slate-900 px-3 py-1 text-sm font-medium text-white disabled:opacity-50 dark:bg-slate-100 dark:text-slate-900"
        >
          Create
        </button>
        {error && <p className="text-sm text-red-700 dark:text-red-400">{error}</p>}
      </div>
    </form>
  );
}
