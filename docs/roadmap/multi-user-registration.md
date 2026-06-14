# Roadmap: Public Registration + Admin Approval

**Status:** Planned
**Priority:** Medium
**Depends on:** Password auth (completed)

## Context

Currently the auth system only supports a single admin user created on first-run via the setup wizard. For multi-user deployments, we need public registration with admin approval flow.

## Requirements

### User Registration
- Anyone can register via `POST /auth/register` (already exists)
- New users start with status `pending` — cannot log in until approved
- Add `status` field to User model: `active` | `pending` | `rejected`
- Pending user login → return error `Account pending approval`

### Admin Approval
- New endpoints:
  - `GET /admin/users?status=pending` — list pending users
  - `PUT /admin/users/{id}/approve` — approve a pending user
  - `PUT /admin/users/{id}/reject` — reject (with optional reason)
- Only `is_super_admin` users can access these endpoints

### Frontend
- Add Register tab/button on the Login form (always visible, not just on first-run)
- Show pending status message after registration: "Your account is pending admin approval"
- Admin settings page: show pending users list with approve/reject buttons
- Remove first-run-only register logic (setup-status endpoint stays for empty DB detection)

### Database
- Migration: add `status` column to `users` table (default `active`, to not break existing users)
- Existing admin user stays `active`

## Out of Scope
- Email verification
- Password reset email flow
- Role-based access control (beyond admin/member)
- Invite links
