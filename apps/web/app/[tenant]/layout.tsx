import { notFound } from "next/navigation";
import { isTenantSlug } from "@/lib/tenant-paths";

type Props = { children: React.ReactNode; params: Promise<{ tenant: string }> };

/**
 * Every tenant page sits under its slug (`/lab/fish`). Anything else in that
 * place -- a file a browser asks for (`/favicon.ico`), a reserved word -- is a
 * 404 here, before any page signs the visitor in or redirects them.
 */
export default async function TenantLayout({ children, params }: Props) {
  const { tenant } = await params;
  if (!isTenantSlug(tenant)) notFound();
  return children;
}
