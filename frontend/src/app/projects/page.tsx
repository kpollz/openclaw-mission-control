"use client";

export const dynamic = "force-dynamic";

import { useMemo, useState } from "react";
import Link from "next/link";

import { useAuth } from "@/auth/clerk";
import { useQueryClient } from "@tanstack/react-query";

import { ApiError } from "@/api/mutator";
import {
  type listProjectsApiV1ProjectsGetResponse,
  getListProjectsApiV1ProjectsGetQueryKey,
  useDeleteProjectApiV1ProjectsProjectIdDelete,
  useListProjectsApiV1ProjectsGet,
} from "@/api/generated/projects/projects";
import { createOptimisticListDeleteMutation } from "@/lib/list-delete";
import { useOrganizationMembership } from "@/lib/use-organization-membership";
import { useUrlSorting } from "@/lib/use-url-sorting";
import type { ProjectRead } from "@/api/generated/model";
import { BoardsTable } from "@/components/boards/BoardsTable";
import { DashboardPageLayout } from "@/components/templates/DashboardPageLayout";
import { buttonVariants } from "@/components/ui/button";
import { ConfirmActionDialog } from "@/components/ui/confirm-action-dialog";

const PROJECT_SORTABLE_COLUMNS = ["name", "updated_at"];

export default function ProjectsPage() {
  const { isSignedIn } = useAuth();
  const queryClient = useQueryClient();
  const { sorting, onSortingChange } = useUrlSorting({
    allowedColumnIds: PROJECT_SORTABLE_COLUMNS,
    defaultSorting: [{ id: "name", desc: false }],
    paramPrefix: "projects",
  });

  const { isAdmin } = useOrganizationMembership(isSignedIn);
  const [deleteTarget, setDeleteTarget] = useState<ProjectRead | null>(null);

  const boardsKey = getListProjectsApiV1ProjectsGetQueryKey();
  const boardsQuery = useListProjectsApiV1ProjectsGet<
    listProjectsApiV1ProjectsGetResponse,
    ApiError
  >(undefined, {
    query: {
      enabled: Boolean(isSignedIn),
      refetchInterval: 30_000,
      refetchOnMount: "always",
    },
  });

  const boards = useMemo(
    () =>
      boardsQuery.data?.status === 200
        ? (boardsQuery.data.data.items ?? [])
        : [],
    [boardsQuery.data],
  );

  const deleteMutation = useDeleteProjectApiV1ProjectsProjectIdDelete<
    ApiError,
    { previous?: listProjectsApiV1ProjectsGetResponse }
  >(
    {
      mutation: createOptimisticListDeleteMutation<
        ProjectRead,
        listProjectsApiV1ProjectsGetResponse,
        { projectId: string }
      >({
        queryClient,
        queryKey: boardsKey,
        getItemId: (board) => board.id,
        getDeleteId: ({ projectId }) => projectId,
        onSuccess: () => {
          setDeleteTarget(null);
        },
        invalidateQueryKeys: [boardsKey],
      }),
    },
    queryClient,
  );

  const handleDelete = () => {
    if (!deleteTarget) return;
    deleteMutation.mutate({ projectId: deleteTarget.id });
  };

  return (
    <>
      <DashboardPageLayout
        signedOut={{
          message: "Sign in to view projects.",
          forceRedirectUrl: "/projects",
          signUpForceRedirectUrl: "/projects",
        }}
        title="Projects"
        description={`Manage projects and task workflows. ${boards.length} project${boards.length === 1 ? "" : "s"} total.`}
        headerActions={
          boards.length > 0 && isAdmin ? (
            <Link
              href="/projects/new"
              className={buttonVariants({
                size: "md",
                variant: "primary",
              })}
            >
              Create project
            </Link>
          ) : null
        }
        stickyHeader
      >
        <div className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
          <BoardsTable
            boards={boards}
            isLoading={boardsQuery.isLoading}
            sorting={sorting}
            onSortingChange={onSortingChange}
            showActions
            stickyHeader
            onDelete={setDeleteTarget}
            emptyState={{
              title: "No projects yet",
              description:
                "Create your first project to start routing tasks and monitoring work across agents.",
              actionHref: "/projects/new",
              actionLabel: "Create your first project",
            }}
          />
        </div>

        {boardsQuery.error ? (
          <p className="mt-4 text-sm text-red-500">
            {boardsQuery.error.message}
          </p>
        ) : null}
      </DashboardPageLayout>
      <ConfirmActionDialog
        open={!!deleteTarget}
        onOpenChange={(open) => {
          if (!open) {
            setDeleteTarget(null);
          }
        }}
        ariaLabel="Delete project"
        title="Delete project"
        description={
          <>
            This will remove {deleteTarget?.name}. This action cannot be undone.
          </>
        }
        errorMessage={deleteMutation.error?.message}
        onConfirm={handleDelete}
        isConfirming={deleteMutation.isPending}
      />
    </>
  );
}
