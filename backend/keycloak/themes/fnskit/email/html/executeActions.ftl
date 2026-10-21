<#outputformat "plainText">
<#assign requiredActionsText><#if requiredActions??><#list requiredActions><#items as reqActionItem>${msg("requiredAction.${reqActionItem}")}<#sep>, </#sep></#items></#list></#if></#assign>
</#outputformat>

<#import "template.ftl" as layout>
<@layout.emailLayout title=msg("fnskitActionsTitle")>
  <p>${msg("fnskitActionsLead", realmName, requiredActionsText)}</p>
  <@layout.button href=link label=msg("fnskitActionsButton") />
  <@layout.linkNote expiration=linkExpirationFormatter(linkExpiration) />
  <p style="margin:0;font-size:13px;color:#5C5C58;">${msg("fnskitActionsIgnore")}</p>
</@layout.emailLayout>
