from django.urls import path

from .views import RegisterView, LoginView, VerifyEmailView, PasswordRecoveryView, PasswordChangeView, \
    CustomTokenRefreshView, ResendVerificationView, LogoutView

urlpatterns = [
    path('register/', RegisterView.as_view(), name='register'),
    path('verify-email/', VerifyEmailView.as_view(), name='verify'),
    path("resend-verification/", ResendVerificationView.as_view(), name="resend-verification"),
    path('login/', LoginView.as_view(), name='login'),
    path('token/refresh/', CustomTokenRefreshView.as_view(), name='token_refresh'),
    path('logout/', LogoutView.as_view(), name='logout'),
    path('password-recovery/', PasswordRecoveryView.as_view(), name='password-recovery'),
    path('password-change/', PasswordChangeView.as_view(), name='password-change'),
]
