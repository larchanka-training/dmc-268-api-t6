import { apiClient } from "../../api/client";
import { parseProject, type Project } from "../../api/types";

export async function renameProject(projectId: string, name: string): Promise<Project> {
  const payload = await apiClient.post(`/api/projects/${encodeURIComponent(projectId)}/rename`, {
    name,
  });
  return parseProject(payload);
}
