"""Expand the unreleased PowerPoint catalog with verified content tools."""

from alembic import op

revision = "0013_powerpoint_tools"
down_revision = "0012_powerpoint_analysis"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_slide_actions_content_type"), "slide_actions", type_="check")
    op.create_check_constraint(
        op.f("ck_slide_actions_content_type"),
        "slide_actions",
        "(source_format='pdf' AND action_type IN ('zoom_region','highlight_region')) OR "
        "(source_format='pptx' AND action_type IN ('highlight_region','draw_rectangle',"
        "'draw_oval','draw_underline','draw_arrow','callout','draw_line','filled_rectangle',"
        "'filled_oval','text_box','draw_rounded_rectangle','draw_diamond','draw_triangle',"
        "'draw_hexagon','draw_star'))",
    )


def downgrade() -> None:
    raise RuntimeError(
        "Forward-only migration; restore a verified backup or apply a reviewed repair."
    )
