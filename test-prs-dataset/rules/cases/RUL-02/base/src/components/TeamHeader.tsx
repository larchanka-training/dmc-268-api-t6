import type { Team } from "../hooks/useTeam";
import { t } from "../i18n";

export function TeamHeader({ team }: { team: Team }) {
  return (
    <header>
      <h1>{t("team.header.title", { name: team.name })}</h1>
      <p>{t("team.header.members", { count: String(team.members.length) })}</p>
    </header>
  );
}
