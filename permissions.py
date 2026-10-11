"""Shared runtime authorization for bot administration."""


async def can_administer(interaction) -> bool:
    if interaction.permissions.administrator:
        return True
    if not getattr(interaction, "guild_id", None):
        return False
    client = getattr(interaction, "client", None)
    if client is None:
        return False
    role_id = await client.database.get_role(interaction.guild_id, "bot_admin")
    return role_id is not None and any(
        role.id == role_id for role in getattr(interaction.user, "roles", ())
    )
