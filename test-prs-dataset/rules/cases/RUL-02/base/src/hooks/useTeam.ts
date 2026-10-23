import { useCallback, useEffect, useState } from "react";

import { apiClient } from "../api/client";

export type Member = { id: string; login: string };
export type Team = { id: string; name: string; members: Member[] };

export function useTeam(teamId: string) {
  const [team, setTeam] = useState<Team>();
  const path = `/api/teams/${encodeURIComponent(teamId)}`;

  useEffect(() => {
    let active = true;
    void apiClient.get(path).then((data) => {
      if (active) {
        setTeam(data as Team);
      }
    });
    return () => {
      active = false;
    };
  }, [path]);

  const removeMember = useCallback(
    async (memberId: string) => {
      await apiClient.post(`${path}/members/${encodeURIComponent(memberId)}/remove`);
      setTeam((current) =>
        current && { ...current, members: current.members.filter((m) => m.id !== memberId) },
      );
    },
    [path],
  );

  return { team, removeMember };
}
