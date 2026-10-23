import { ProjectGeneral } from "../components/ProjectGeneral";
import { useProject } from "../hooks/useProject";
import { t } from "../i18n";

export function ProjectSettingsPage({ projectId }: { projectId: string }) {
  const project = useProject(projectId);
  if (project === undefined) {
    return <p>{t("common.loading")}</p>;
  }
  return (
    <main>
      <h1>{t("project.settings.title")}</h1>
      <ProjectGeneral project={project} />
    </main>
  );
}
