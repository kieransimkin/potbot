import { OrbWorkspace } from "@/components/OrbWorkspace";

export default async function Home({ searchParams }: { searchParams: Promise<{ demo?: string }> }) {
  const { demo } = await searchParams;
  // Explicit development-only fixture. A production deploy cannot turn demo on via URL.
  const demoMode = process.env.NODE_ENV !== "production" && demo === "1";
  return <OrbWorkspace demo={demoMode} />;
}
