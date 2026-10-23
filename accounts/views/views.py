from django.contrib.auth.models import auth
from django.http import HttpResponse
from django.shortcuts import render, redirect
from django.template.loader import get_template




def login(request):
    return render(request, "accounts/login.html")

def logout(request):
    auth.logout(request)
    return redirect("projects:landing_page")


def error_404(request, exception):
    """Custom 404 page"""
    return render(request, "accounts/error/error_404.html", status=404)

def error_500(request):
    """Custom 500 page"""
    template = get_template("accounts/error/error_500.html")
    html = template.render({})
    return HttpResponse(html, status=500)

def error_403(request, exception):
    """Custom 403 page"""
    return render(request, "accounts/error/error_403.html", status=403)

