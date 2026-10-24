import { TeamHeader } from "../components/TeamHeader";
import { useTeam } from "../hooks/useTeam";

export function TeamPage({ teamId }: { teamId: string }) {
  const { team } = useTeam(teamId);
  if (team === undefined) {
    return null;
  }
  return (
    <main>
      <TeamHeader team={team} />
    </main>
  );
}
