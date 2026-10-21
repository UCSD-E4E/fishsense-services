import { redirect } from "next/navigation";
import { paths } from "@/lib/tenant-paths";

type Props = { params: Promise<{ tenant: string }> };

/** The capture list was retired (phase 7b): a phone's fish are in the Fish
 *  list and in their missions. An old link lands on the phone's missions. */
export default async function Page({ params }: Props) {
  const { tenant } = await params;
  redirect(paths.phoneMissions(tenant));
}
