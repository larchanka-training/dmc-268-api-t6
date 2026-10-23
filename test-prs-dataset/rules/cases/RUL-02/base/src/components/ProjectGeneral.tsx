import type { Project } from "../hooks/useProject";
import { t } from "../i18n";

export function ProjectGeneral({ project }: { project: Project }) {
  return (
    <section>
      <h2>{t("project.general.title")}</h2>
      <p>{project.name}</p>
    </section>
  );
}
