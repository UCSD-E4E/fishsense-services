import { redirect } from "next/navigation";
import { paths } from "@/lib/tenant-paths";

type Props = { params: Promise<{ tenant: string }> };

/** A tenant's home is its Fish until Today is built (docs/design/README.md). */
export default async function TenantHome({ params }: Props) {
  const { tenant } = await params;
  redirect(paths.fish(tenant));
}
