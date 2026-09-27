from .models import ProfileState


def render_brief(state: ProfileState, courses_md: str | None) -> str:
    """Render the short tutor-facing study brief."""
    raise NotImplementedError


def render_profile(state: ProfileState) -> str:
    """Render the complete learner profile as Markdown."""
    raise NotImplementedError
