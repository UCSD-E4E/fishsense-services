import { PickTenant } from "@/app/_shell/pick-tenant";

export const dynamic = "force-dynamic";

type Props = { searchParams: Promise<Record<string, string | string[] | undefined>> };

/** Moved under its tenant (`/lab/...`): an old link that names none lands here. */
export default async function Page({ searchParams }: Props) {
  return <PickTenant path="/fish" search={await searchParams} />;
}
