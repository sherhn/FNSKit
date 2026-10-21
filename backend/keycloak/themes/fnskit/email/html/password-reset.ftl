<#import "template.ftl" as layout>
<@layout.emailLayout title=msg("fnskitResetTitle")>
  <p>${msg("fnskitResetLead", realmName)}</p>
  <@layout.button href=link label=msg("fnskitResetButton") />
  <@layout.linkNote expiration=linkExpirationFormatter(linkExpiration) />
  <p style="margin:0 0 10px;font-size:13px;color:#5C5C58;">${msg("fnskitResetOtp")}</p>
  <p style="margin:0;font-size:13px;color:#5C5C58;">${msg("fnskitResetIgnore")}</p>
</@layout.emailLayout>
