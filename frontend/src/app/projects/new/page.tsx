"use client";

export const dynamic = "force-dynamic";

import { useMemo, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";

import { useAuth } from "@/auth/clerk";

import { ApiError } from "@/api/mutator";
import { useCreateProjectApiV1ProjectsPost } from "@/api/generated/projects/projects";
import {
  type listGatewaysApiV1GatewaysGetResponse,
  useListGatewaysApiV1GatewaysGet,
} from "@/api/generated/gateways/gateways";
import { useOrganizationMembership } from "@/lib/use-organization-membership";
import { DashboardPageLayout } from "@/components/templates/DashboardPageLayout";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import SearchableSelect from "@/components/ui/searchable-select";
import { Textarea } from "@/components/ui/textarea";

const slugify = (value: string) =>
  value
    .toLowerCase()
    .trim()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/(^-|-$)/g, "") || "project";

export default function NewProjectPage() {
  const router = useRouter();
  const { isSignedIn } = useAuth();

  const { isAdmin } = useOrganizationMembership(isSignedIn);

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [gatewayId, setGatewayId] = useState<string>("");

  const [error, setError] = useState<string | null>(null);

  const gatewaysQuery = useListGatewaysApiV1GatewaysGet<
    listGatewaysApiV1GatewaysGetResponse,
    ApiError
  >(undefined, {
    query: {
      enabled: Boolean(isSignedIn && isAdmin),
      refetchOnMount: "always",
      retry: false,
    },
  });

  const createBoardMutation = useCreateProjectApiV1ProjectsPost<ApiError>({
    mutation: {
      onSuccess: (result) => {
        if (result.status === 200) {
          router.push(`/projects/${result.data.id}/edit?onboarding=1`);
        }
      },
      onError: (err) => {
        setError(err.message || "Something went wrong.");
      },
    },
  });

  const gateways = useMemo(() => {
    if (gatewaysQuery.data?.status !== 200) return [];
    return gatewaysQuery.data.data.items ?? [];
  }, [gatewaysQuery.data]);
  const displayGatewayId = gatewayId || gateways[0]?.id || "";
  const isLoading =
    gatewaysQuery.isLoading ||
    createBoardMutation.isPending;
  const errorMessage =
    error ?? gatewaysQuery.error?.message ?? null;

  const isFormReady = Boolean(
    name.trim() && description.trim() && displayGatewayId,
  );

  const gatewayOptions = useMemo(
    () =>
      gateways.map((gateway) => ({ value: gateway.id, label: gateway.name })),
    [gateways],
  );

  const handleSubmit = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!isSignedIn) return;
    const trimmedName = name.trim();
    const resolvedGatewayId = displayGatewayId;
    if (!trimmedName) {
      setError("Project name is required.");
      return;
    }
    if (!resolvedGatewayId) {
      setError("Select a gateway before creating a project.");
      return;
    }
    const trimmedDescription = description.trim();
    if (!trimmedDescription) {
      setError("Project description is required.");
      return;
    }

    setError(null);

    createBoardMutation.mutate({
      data: {
        name: trimmedName,
        slug: slugify(trimmedName),
        description: trimmedDescription,
        gateway_id: resolvedGatewayId,
      },
    });
  };

  return (
    <DashboardPageLayout
      signedOut={{
        message: "Sign in to create a project.",
        forceRedirectUrl: "/projects/new",
        signUpForceRedirectUrl: "/projects/new",
      }}
      title="Create project"
      description="Projects organize tasks and agents by mission context."
      isAdmin={isAdmin}
      adminOnlyMessage="Only organization owners and admins can create projects."
    >
      <form
        onSubmit={handleSubmit}
        className="space-y-6 rounded-xl border border-slate-200 bg-white p-6 shadow-sm"
      >
        <div className="space-y-4">
          <div className="grid gap-6 md:grid-cols-2">
            <div className="space-y-2">
              <label className="text-sm font-medium text-slate-900">
                Project name <span className="text-red-500">*</span>
              </label>
              <Input
                value={name}
                onChange={(event) => setName(event.target.value)}
                placeholder="e.g. Release operations"
                disabled={isLoading}
              />
            </div>
            <div className="space-y-2">
              <label className="text-sm font-medium text-slate-900">
                Gateway <span className="text-red-500">*</span>
              </label>
              <SearchableSelect
                ariaLabel="Select gateway"
                value={displayGatewayId}
                onValueChange={setGatewayId}
                options={gatewayOptions}
                placeholder="Select gateway"
                searchPlaceholder="Search gateways..."
                emptyMessage="No gateways found."
                triggerClassName="w-full h-11 rounded-xl border border-slate-300 bg-white px-3 py-2 text-sm font-medium text-slate-900 shadow-sm focus:border-blue-500 focus:ring-2 focus:ring-blue-200"
                contentClassName="rounded-xl border border-slate-200 shadow-lg"
                itemClassName="px-4 py-3 text-sm text-slate-700 data-[selected=true]:bg-slate-50 data-[selected=true]:text-slate-900"
              />
            </div>
          </div>

          <div className="space-y-2">
            <label className="text-sm font-medium text-slate-900">
              Description <span className="text-red-500">*</span>
            </label>
            <Textarea
              value={description}
              onChange={(event) => setDescription(event.target.value)}
              placeholder="What context should the lead agent know before onboarding?"
              className="min-h-[120px]"
              disabled={isLoading}
            />
          </div>
        </div>

        {gateways.length === 0 ? (
          <div className="rounded-lg border border-slate-200 bg-slate-50 px-4 py-3 text-sm text-slate-600">
            <p>
              No gateways available. Create one in{" "}
              <Link
                href="/gateways"
                className="font-medium text-blue-600 hover:text-blue-700"
              >
                Gateways
              </Link>{" "}
              to continue.
            </p>
          </div>
        ) : null}

        {errorMessage ? (
          <p className="text-sm text-red-500">{errorMessage}</p>
        ) : null}

        <div className="flex justify-end gap-3">
          <Button
            type="button"
            variant="ghost"
            onClick={() => router.push("/projects")}
            disabled={isLoading}
          >
            Cancel
          </Button>
          <Button type="submit" disabled={isLoading || !isFormReady}>
            {isLoading ? "Creating…" : "Create project"}
          </Button>
        </div>
      </form>
    </DashboardPageLayout>
  );
}
