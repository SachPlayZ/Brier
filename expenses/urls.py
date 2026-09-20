from django.contrib.auth import views as auth_views
from django.urls import path
from expenses import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("landing/", views.landing, name="landing"),
    path("claims/", views.claim_list, name="claim_list"),
    path("claims/new/", views.claim_submit, name="claim_submit"),
    path("claims/<str:claim_id>/", views.claim_detail, name="claim_detail"),
    path("claims/<str:claim_id>/decide/", views.decide, name="decide"),
    path("review/", views.review_queue, name="review_queue"),
    path("duplicates/<int:flag_id>/", views.dup_compare, name="dup_compare"),
    path("fields/<int:field_id>/correct/", views.correct_field, name="correct_field"),
    path("signup/", views.signup, name="signup"),
    path("login/", views.CustomLoginView.as_view(), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
]
